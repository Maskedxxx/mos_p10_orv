from copy import deepcopy
import json
import logging

import pytest
from pydantic import ValidationError

from app.contracts import DataRequest, HypothesisMap, MapRow, RequestItem, RequestOutput
from app.mandatory_indicators import MandatoryMetric
from app.nodes.request import _ensure_mandatory, _append, _check_coverage, _order_and_number


def source(row, metric):
    return {"row_id": row, "metric": metric}


def item(num, metric, sources, **kwargs):
    return {"num": str(num), "kind": "показатель по годам", "metric": metric, "unit": "ед.",
            "for_rows": sorted({s["row_id"] for s in sources}), "sources": sources, **kwargs}


def validate(items, sources, mandatory=None):
    return RequestOutput.model_validate({"items": items, "years": [2023, 2024, 2025]},
        context={"request_sources": sources, "request_mandatory": mandatory or []})


def test_standalone_counter_can_be_covered_by_breakdown_with_all_provenance():
    sources = [source(2, "Отказы по всем основаниям"), source(9, "Отказы по основанию А")]
    result = validate([item(1, "Количество отказов", sources, breakdown=["Основание А", "Основание Б"],
                            note="Источник строки 9 покрыт подстрокой 1.1")], sources)
    assert len(result.items) == 1
    assert result.items[0].for_rows == [2, 9]
    assert len(result.items[0].sources) == 2


def test_narrower_counter_is_not_removed_as_similar():
    sources = [source(2, "Все отказы по основанию А"), source(9, "Отказы по основанию А только для подкатегории")]
    rows = [item(1, "Количество отказов", sources[:1], breakdown=["Основание А", "Основание Б"], note="Все категории"),
            item(2, "Количество отказов в подкатегории", sources[1:], note="Только указанная подкатегория")]
    result = validate(rows, sources)
    assert len(result.items) == 2
    assert result.items[1].metric == rows[1]["metric"]


def test_legitimate_split_keeps_same_source_without_hard_error(valid_map_row, caplog):
    src = source(4, "Количество получателей и сумма выплат")
    rows = [item(1, "Количество получателей", [src]), item(2, "Сумма выплат", [src], unit="руб.")]
    result = validate(rows, [src])
    data = deepcopy(valid_map_row)
    data.update(row_id=4, data=[{"metric": src["metric"], "source": "запрос органу"}])
    hyp_map = HypothesisMap(rows=[MapRow(**data)])
    with caplog.at_level(logging.WARNING):
        _check_coverage(hyp_map, result, logging.getLogger("request-split"))
    assert len(result.items) == 2
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_same_source_with_different_periods_and_units_is_not_semantically_deduplicated():
    src = source(4, "Сведения для сопоставимых периодов")
    result = validate([item(1, "Количество случаев до изменения", [src]),
                       item(2, "Количество случаев после изменения", [src]),
                       item(3, "Расходы за период", [src], unit="руб.")], [src])
    assert len(result.items) == 3


@pytest.mark.parametrize("wrong", [source(99, "Показатель"), source(1, "Пересказ исходного показателя"), source(1, "Открытые сведения")])
def test_only_exact_transmitted_source_pairs_are_accepted(wrong):
    with pytest.raises(ValidationError, match="sources"):
        validate([item(1, "Число случаев", [wrong])], [source(1, "Показатель")])


def test_question_to_clarify_numeric_breakdown_can_reference_numeric_source():
    src = source(1, "Количество объектов по категориям")
    question = item(1, "Какие категории применяются при учёте объектов?", [src],
                    kind="открытый вопрос", unit=None, clause="п. 2")
    assert validate([question], [src])


def test_mandatory_codes_completed_without_duplicate_measurement():
    entry = MandatoryMetric(codes=["A", "B"], metric="Средняя продолжительность", unit="рабочих дней", breakdown="", kind="показатель по годам")
    original = DataRequest(items=[RequestItem(**item(1, entry.metric, [], unit=entry.unit, mandatory=["A"]))], years=[2025])
    result = _ensure_mandatory(original, [entry], logging.getLogger("mandatory"))
    assert len(result.items) == 1 and result.items[0].mandatory == ["A", "B"]
    assert original.items[0].mandatory == ["A"]
    assert result.items[0].metric == entry.metric and result.items[0].unit == entry.unit


def test_mandatory_form_can_exist_without_map_sources():
    entry = dict(codes=["A"], metric="Средняя продолжительность", unit="рабочих дней", kind="показатель по годам", breakdown="")
    assert validate([item(1, entry["metric"], [], unit="рабочих дней", mandatory=["A"])], [], [entry])
    for change in ({"mandatory": ["unknown"]}, {"metric": "Число процедур"}, {"unit": "ед."}):
        with pytest.raises(ValidationError):
            validate([item(1, entry["metric"], [], unit="рабочих дней", mandatory=["A"]) | change], [], [entry])


def test_one_position_can_cover_mandatory_and_map_sources():
    src = source(7, "Число обращений")
    entry = dict(codes=["A"], metric="Количество обращений", unit="ед.", kind="показатель по годам", breakdown="")
    result = validate([item(1, entry["metric"], [src], mandatory=["A"])], [src], [entry])
    assert result.items[0].mandatory == ["A"] and result.items[0].for_rows == [7]


def test_append_preserves_existing_rows_breakdown_note_and_years():
    old = DataRequest(items=[RequestItem(**item(8, "Число решений", [source(1, "Решения")], breakdown=["Категория А"], note="Сохранить пояснение"))], years=[2021])
    new = DataRequest(items=[RequestItem(**item(1, "Число новых случаев", [source(3, "Новые случаи")]))], years=[2099], why_dropped="Иное число покрыто позицией 8.1")
    before = old.model_dump(mode="json")
    result = _append(old, new)
    assert result.items[0].model_dump(mode="json") == before["items"][0]
    assert result.items[1].num == "9" and result.years == [2021]
    assert old.model_dump(mode="json") == before


def test_postprocessing_changes_only_authorized_fields_and_missing_mandatory():
    raw = DataRequest(items=[RequestItem(**item(1, "Счётчик", [source(3, "Счётчик")]))], years=[1900])
    entry = MandatoryMetric(codes=["A"], metric="Обязательный показатель", unit="ед.", breakdown="по установленным видам", kind="показатель по годам")
    before = raw.model_dump(mode="json")
    result = _order_and_number(_ensure_mandatory(raw.model_copy(update={"years": [2023, 2024, 2025]}), [entry], logging.getLogger("mandatory")))
    assert raw.model_dump(mode="json") == before
    assert result.years == [2023, 2024, 2025]
    assert result.items[0].metric == entry.metric and result.items[0].mandatory == ["A"]
    assert result.items[1].model_dump(exclude={"num"}) == raw.items[0].model_dump(exclude={"num"})


def test_archive_request_does_not_need_input_context():
    archived = DataRequest(items=[RequestItem(**item(8, "Счётчик", [source(77, "Счётчик")]))], years=[2020])
    assert DataRequest.model_validate_json(archived.model_dump_json()).items[0].num == "8"


def test_missing_sources_require_explanation_but_project_and_append_exclusions_are_allowed():
    src = source(1, "Статистика применения")
    with pytest.raises(ValidationError, match="why_dropped"):
        validate([], [src])
    for why in ("Строка 1: статистика применения отсутствует для ещё не действующего проекта",
                "Строка 1: показатель покрыт существующей позицией 8.1"):
        result = RequestOutput.model_validate({"items": [], "years": [2025], "why_dropped": why},
                                             context={"request_sources": [src], "request_mandatory": []})
        assert result.items == [] and result.why_dropped == why
