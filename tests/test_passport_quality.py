"""Passport corrections: fresh answers are checked, archives remain readable."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.contracts import ActPassport, ActPassportOutput, Developer
from app.nodes.passport import apply_user_developers


@pytest.mark.parametrize("reason", [None, "", " \n\t"])
def test_new_empty_developers_need_explanation_but_old_passports_read(valid_passport, reason):
    data = deepcopy(valid_passport)
    if reason is not None:
        data["requisites"]["developers_absence_reason"] = reason
    assert ActPassport.model_validate(data).requisites.developers == []
    with pytest.raises(ValidationError, match="developers_absence_reason"):
        ActPassportOutput.model_validate(data)


@pytest.mark.parametrize("reason", [
    "В переданном тексте орган применения не определён.",
    "Назван только подписант, исполнение ему не поручено.",
    "Участник процедуры ведёт техническую передачу; орган применения не установлен.",
])
@pytest.mark.parametrize("has_procedure", [False, True])
def test_explained_absence_is_valid_even_with_named_participants(valid_passport, reason, has_procedure):
    data = deepcopy(valid_passport)
    data["requisites"]["developers_absence_reason"] = reason
    if has_procedure:
        data["procedure"].update(state="есть", participants="Заявитель и технический участник",
                                 clauses=["п. 2–4"])
        data["addressee"]["scope"] = "и тем и другим"
    passport = ActPassportOutput.model_validate(data)
    assert passport.requisites.developers == []
    assert passport.requisites.developers_absence_reason == reason


@pytest.mark.parametrize("name", ["Уполномоченный департамент", "Коллегиальный совет", "Орган с иной ролью"])
def test_listed_developer_does_not_require_absence_reason_or_name_heuristic(valid_passport, name):
    data = deepcopy(valid_passport)
    data["requisites"]["developers"] = [dict(name=name, scope="раздел 1", clauses="п. 3", basis="Рассматривает обращения")]
    result = ActPassportOutput.model_validate(data)
    assert result.requisites.developers[0].name == name
    assert result.requisites.developers_absence_reason is None


def test_user_choice_clears_previous_absence_reason_without_mutating_original(valid_passport):
    data = deepcopy(valid_passport)
    data["requisites"]["developers_absence_reason"] = "Не удалось установить из текста."
    original = ActPassportOutput.model_validate(data)
    changed = apply_user_developers(original, ["Орган А", "Орган Б"])
    assert [d.name for d in changed.requisites.developers] == ["Орган А", "Орган Б"]
    assert changed.requisites.developers_absence_reason is None
    assert original.requisites.developers == []
    assert original.requisites.developers_absence_reason == "Не удалось установить из текста."


@pytest.mark.parametrize("kind,amends,valid", [
    ("акт изменений не вносит", False, True),
    ("акт изменений не вносит", True, False),
    ("только изменяющий акт", False, False),
    ("только изменяющий акт", True, True),
    ("консолидированная редакция", False, True),
    # A consolidated document may itself amend another act; don't guess from text.
    ("консолидированная редакция", True, True),
    ("и то и другое", False, False),
    ("и то и другое", True, True),
    ("утверждает своё и изменяет другие акты", False, False),
    ("утверждает своё и изменяет другие акты", True, True),
])
def test_input_kind_only_rejects_structural_contradiction(valid_passport, kind, amends, valid):
    data = deepcopy(valid_passport)
    data["nature"]["input_kind"] = kind
    if amends:
        data["nature"]["actions"].append({"action": "вносит изменения", "target": "Другой акт", "clauses": ["п. 2"]})
    if valid:
        assert ActPassport.model_validate(data).nature.input_kind == kind
    else:
        with pytest.raises(ValidationError):
            ActPassport.model_validate(data)


@pytest.mark.parametrize("clauses", [["п. 2–4"], ["п. 2.1", "п. 2.2"], ["п. 2 приложения 1", "п. 2 приложения 2"]])
def test_clause_ranges_and_multiple_appendices_are_not_mechanically_rejected(valid_passport, clauses):
    data = deepcopy(valid_passport)
    data["requisites"]["developers_absence_reason"] = "Исполнитель в переданном фрагменте не установлен."
    data["procedure"].update(state="есть", clauses=clauses, participants=None)
    data["addressee"]["scope"] = "и тем и другим"
    result = ActPassportOutput.model_validate(data)
    assert result.procedure.clauses == clauses
    assert result.procedure.participants is None


def test_model_schema_contains_domain_task_without_internal_history():
    schema = ActPassportOutput.model_json_schema()
    description = schema["description"]
    assert "структурное описание" in description
    for token in ("код-ревью", "15.10", "итерации", "ActPassportOutput", "Сохранённый паспорт"):
        assert token not in description
    assert "коллегиального органа" in Developer.model_fields["name"].description
