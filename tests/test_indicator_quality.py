from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.contracts import (Indicator01Output, Indicator10Output, Indicator11Output,
                           CheckedReference, ComparedRequirement, DeadlineCheck, PracticeFinding)
from app.config import NO_DATA_TEXT_FAMILY


def verdict(n, value):
    return dict(indicator=n, name="Индикатор", rationale="Разбор по проверенным данным", verdict=value,
                rationale_brief=["Результат проверки"], missing_data="Нужны сведения" if value == "нет данных" else None)


def document(held=True, requested=False, required=True):
    return dict(name="Документ", clause="п. 2", availability_basis="Основание по тексту",
                held_by_state=held, requested_by_body=requested, required_from_applicant=required)


@pytest.mark.parametrize("held,requested,required,expected", [
    (True, False, True, "нарушен"), (True, True, True, "нарушен"),
    (False, True, True, "нарушен"), (True, True, False, "соблюдён"),
    (True, False, False, "соблюдён"), (False, False, True, "соблюдён"),
    (None, False, True, "нет данных"), (True, None, True, "нет данных"),
    (True, False, None, "нет данных"), (None, None, False, "соблюдён"),
])
def test_document_availability_and_optional_submission(held, requested, required, expected):
    result = Indicator01Output(**verdict(1, expected), checked_documents=[document(held, requested, required)])
    assert result.verdict == expected


def test_proven_document_violation_has_priority_over_other_gaps():
    result = Indicator01Output(**verdict(1, "нарушен"), checked_documents=[document(), document(None)],
                               coverage_gaps=["Часть перечня в недоступном приложении"])
    assert result.verdict == "нарушен"


def test_missing_external_list_is_not_non_applicability():
    Indicator01Output(**verdict(1, "нет данных"), coverage_gaps=["Недоступен внешний перечень"])
    with pytest.raises(ValidationError):
        Indicator01Output(**verdict(1, "не применимо"), coverage_gaps=["Недоступен внешний перечень"])


def test_double_collection_is_no_longer_an_indicator4_mark():
    with pytest.raises(ValidationError):
        ComparedRequirement(requirement="Документ", clause="п. 1", counterpart="этот акт п. 2",
                            comparison="Оба обязаны", relation="требует и сам получает")


@pytest.mark.parametrize("status", ["актуально", "устарело"])
def test_authority_status_needs_read_text_not_only_regulation_status(status):
    item = dict(kind="орган", name="Орган", clause="п. 1", found="Положение действует", status=status,
                source="https://example.org/regulation")
    with pytest.raises(ValidationError, match="read_excerpt"):
        CheckedReference(**item)
    assert CheckedReference(**item, read_excerpt="Фрагмент об органе и названии").status == status
    assert CheckedReference(**(item | {"status": "не проверено", "source": None})).status == "не проверено"
    assert CheckedReference(**(item | {"kind": "акт"})).status == status


def target(i):
    return dict(target_id=i, clause=f"п. {i}", subject="Процедура", actor=None, expected_fact="Результаты применения")


def finding(i, mark="работает"):
    return dict(target_id=i, read_status="прочитано", source="https://example.org/report", excerpt="Данные отчёта",
                period="за отчётный год", linkage="Соответствует предмету и территории", fact="Факт", about="Проверяемая процедура", mark=mark)


def test_single_positive_finding_does_not_cover_two_targets():
    data = dict(targets=[target(1), target(2)], findings=[finding(1)],
                coverage=[dict(target_id=1, status="подтверждено", finding_indices=[1]),
                          dict(target_id=2, status="не установлено", gap="Нет данных о результате")],
                applicability="применим", applicability_basis="Акт действует", searches=["Поиск по цели"],
                to_request=[dict(target_id=2, metric="Число решений", unit="ед.", clause="п. 2")])
    assert Indicator10Output(**verdict(10, "нет данных"), **data).verdict == "нет данных"
    with pytest.raises(ValidationError):
        Indicator10Output(**verdict(10, "соблюдён"), **(data | {"to_request": []}))
    # A proven negative finding takes priority, without needing to fill every other gap.
    data["findings"] = [finding(1, "не работает")]
    data["to_request"] = []
    assert Indicator10Output(**verdict(10, "нарушен"), **data).verdict == "нарушен"


@pytest.mark.parametrize("change", [{"read_status": "не прочитано"}, {"excerpt": None}, {"period": None}, {"linkage": " "}])
def test_positive_finding_requires_read_evidence(change):
    with pytest.raises(ValidationError):
        PracticeFinding(**(finding(1) | change))


def test_unread_or_inconclusive_material_can_be_recorded_without_claiming_success():
    assert PracticeFinding(**(finding(1) | {"read_status": "не прочитано", "excerpt": None,
                                             "period": None, "mark": "недостаточно для вывода"}))


def test_coverage_cannot_cite_another_target():
    data = dict(targets=[target(1), target(2)], findings=[finding(1)],
                coverage=[dict(target_id=1, status="подтверждено", finding_indices=[1]),
                          dict(target_id=2, status="подтверждено", finding_indices=[1])],
                applicability="применим", applicability_basis="Акт действует")
    with pytest.raises(ValidationError, match="другой цели"):
        Indicator10Output(**verdict(10, "соблюдён"), **data)


def deadline(status="норма прочитана", mark="в пределах"):
    return dict(whose="орган", what="Рассмотрение", value="срок из акта", clause="п. 1", starts_at="поступление заявления",
                check_status=status, federal="Итог поиска", federal_source="https://example.org/law" if status == "норма прочитана" else None,
                federal_quote="Формулировка нормы" if status == "норма прочитана" else None,
                federal_limit="Предел в тех же единицах" if status == "норма прочитана" else None,
                comparison_basis="То же действие и начало отсчёта", mark=mark)


@pytest.mark.parametrize("status", ["не найдена", "источник недоступен", "несопоставима"])
def test_unfinished_federal_check_is_no_data(status):
    item = deadline(status, "не проверено")
    assert Indicator11Output(**verdict(11, "нет данных"), deadlines=[item])
    with pytest.raises(ValidationError):
        DeadlineCheck(**(item | {"mark": "в пределах"}))
    with pytest.raises(ValidationError):
        Indicator11Output(**verdict(11, "соблюдён"), deadlines=[item])


@pytest.mark.parametrize("field", ["federal_source", "federal_quote", "federal_limit", "comparison_basis"])
def test_federal_comparison_requires_all_explicit_evidence(field):
    with pytest.raises(ValidationError):
        DeadlineCheck(**(deadline() | {field: None}))


def test_proven_deadline_or_internal_violation_has_priority_over_unchecked_norm():
    unknown = deadline("не найдена", "не проверено")
    assert Indicator11Output(**verdict(11, "нет данных"), deadlines=[unknown], units_mixed="Единицы расходятся по пунктам")
    assert Indicator11Output(**verdict(11, "нарушен"), deadlines=[unknown, deadline(mark="длиннее федерального")])
    assert 11 in NO_DATA_TEXT_FAMILY


def test_person_deadline_does_not_need_federal_comparison():
    row = deadline("не требуется", "в пределах") | {"whose": "лицо"}
    assert DeadlineCheck(**row)


def test_evidence_is_written_before_coverage_and_federal_check_status():
    from app.contracts import PracticeCoverage
    fields = list(Indicator10Output.model_fields)
    assert fields.index("targets") < fields.index("searches") < fields.index("findings") < fields.index("coverage") < fields.index("verdict")
    fields = list(PracticeCoverage.model_fields)
    assert fields.index("finding_indices") < fields.index("status") and fields.index("gap") < fields.index("status")
    fields = list(DeadlineCheck.model_fields)
    assert fields.index("federal_quote") < fields.index("check_status") < fields.index("mark")
