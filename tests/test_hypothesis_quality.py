from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from app.contracts import (HypothesisSet, HypothesisSetOutput, NoDataHypothesisSetOutput,
                           NoDataHypothesisOutput, Effect, Verdict)
from app.nodes.hypotheses import _to_request, _verdict_remarks, _verdict_card, _questions_to_data
from app.nodes.merge import flatten_hypotheses, fallback_map
from app.input_contexts import merge_input, request_input
from app.trace import run_dir


def answer(groups):
    return {"indicator": 10, "name": "Практика", "violation_kind": "издержки", "why_this_many": "Разные механизмы",
            "hypotheses": [{"hypothesis": "Неизвестен результат применения нормы", "grounds": "Не найдено данных",
                            "effects": [{"affected": "бизнес", "metric": "Число затронутых обращений, ед.",
                                         "how_to_calculate": "Подсчитать обращения по норме за период",
                                         "expected_trend": "Сведения о результатах позволят закрыть пробел; число само по себе не доказывает причинность",
                                         "how_to_calculate_brief": ["Подсчитать обращения"], "expected_trend_brief": ["Получить сведения о результатах"]}],
                            "data": [{"metric": label, "source": "запрос органу", "origin_request_ids": ids} for label, ids in group],
                            "questions": [], "hypothesis_brief": ["Вопрос по норме"], "grounds_brief": ["Пробел данных"]}
                           for group in groups]}


@pytest.mark.parametrize("model", [HypothesisSetOutput, NoDataHypothesisSetOutput])
def test_reworded_numbers_remain_in_correct_hypotheses_without_extra_additions(model):
    groups = [[("Переформулированный показатель A", ["a"]), ("Показатель Б", ["b"]), ("Показатель В", ["c"])],
              [("Другой показатель", ["d"]), ("Последний показатель", ["e"])]]
    value = model.model_validate(answer(groups), context={"required_request_ids": list("abcde")})
    stored = _questions_to_data(value)
    assert [len(h.data) for h in stored.hypotheses] == [3, 2]
    assert stored.hypotheses[1].data[0].origin_request_ids == ["d"]


@pytest.mark.parametrize("ids,expected", [(["a"], "пропущены"), (["a", "b", "foreign"], "неизвестные")])
def test_coverage_is_checked_by_ids_not_identical_labels(ids, expected):
    with pytest.raises(ValidationError, match=expected):
        NoDataHypothesisSetOutput.model_validate(answer([[("Одинаковое название", ids)]]),
                                                 context={"required_request_ids": ["a", "b"]})


def test_same_counter_may_support_several_hypotheses():
    value = NoDataHypothesisSetOutput.model_validate(answer([[("Счётчик", ["a"])], [("Счётчик", ["a"])]]),
                                                     context={"required_request_ids": ["a"]})
    assert len(value.hypotheses) == 2


def test_open_data_cannot_claim_origin_of_required_organ_counter():
    data = answer([[("Измерение", ["a"])]] )
    data["hypotheses"][0]["data"][0].update(source="открытый", where="Публичный источник")
    with pytest.raises(ValidationError, match="source"):
        NoDataHypothesisSetOutput.model_validate(data, context={"required_request_ids": ["a"]})


def test_no_input_ids_rejects_invented_id_but_archive_has_no_coverage_check():
    raw = answer([[("Измерение", ["invented"])]] )
    with pytest.raises(ValidationError, match="неизвестные"):
        HypothesisSetOutput.model_validate(raw, context={"required_request_ids": []})
    assert HypothesisSetOutput.model_validate(raw)  # no fresh input context for archival reading
    old = answer([[("Измерение", [])]])
    for h in old["hypotheses"]:
        h.pop("questions")
        for d in h["data"]:
            d.pop("origin_request_ids")
    assert HypothesisSet.model_validate(old).hypotheses[0].data[0].origin_request_ids == []


def test_source_ids_stable_scoped_and_source_fields_unchanged(tmp_runs_dir):
    verdict = Verdict(indicator=10, name="Практика", verdict="нет данных", rationale="Пробел", missing_data="Числа")
    items = [{"metric": "Одно название", "unit": "ед.", "clause": "п. 1", "target_id": 1},
             {"metric": "Одно название", "unit": "ед.", "clause": "п. 2", "target_id": 2}]
    path = run_dir("case") / "03_verdicts/10.checks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"to_request": items}))
    before = path.read_bytes()
    first = _to_request("case", verdict)
    assert first == _to_request("case", verdict)
    assert first[0]["request_id"] != first[1]["request_id"]
    assert [{k: v for k, v in d.items() if k != "request_id"} for d in first] == items
    assert path.read_bytes() == before
    items[0]["unit"] = "руб."
    path.write_text(json.dumps({"to_request": items}))
    assert _to_request("case", verdict)[0]["request_id"] != first[0]["request_id"]


def test_all_unclear_wordings_from_accepted_table_reach_card(tmp_runs_dir):
    rows = [{"clause": f"п. {i}", "quote": f"Условие {i}", "readings": "Неопределённость",
             "effect": "Последствие", "mark": "непонятно"} for i in range(1, 7)]
    path = run_dir("case") / "03_verdicts/09.checks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"wordings": rows + [rows[0] | {"mark": "понятно"}]}))
    verdict = Verdict(indicator=9, name="Понятность", verdict="нарушен", rationale="Разбор", rationale_brief=["Существенная деталь"])
    remarks = _verdict_remarks("case", verdict)
    assert remarks == rows
    card = _verdict_card(verdict, remarks=remarks)
    assert "Существенная деталь" in card
    assert json.loads(card.split("## КОНКРЕТНЫЕ ЗАМЕЧАНИЯ ПРОВЕРКИ (JSON)\n\n```json\n")[1].split("\n```")[0]) == rows
    assert _verdict_remarks("case", verdict.model_copy(update={"indicator": 8})) == []
    assert _verdict_remarks("case", verdict.model_copy(update={"verdict": "соблюдён"})) == []


def test_provenance_survives_storage_merge_input_fallback_and_request_view():
    value = NoDataHypothesisSetOutput.model_validate(answer([[("A", ["a"])], [("B", ["b"])]]),
                                                     context={"required_request_ids": ["a", "b"]})
    stored = _questions_to_data(value)
    flat = flatten_hypotheses([HypothesisSet.model_validate_json(stored.model_dump_json())])
    merged_input = merge_input(flat, [])
    assert [h.data[0].origin_request_ids for h in merged_input.hypotheses] == [["a"], ["b"]]
    packet = request_input(fallback_map(flat), [2025], "Акт", None, None, None)
    assert [r.data[0].origin_request_ids for r in packet.rows] == [["a"], ["b"]]


def test_branch_specific_descriptions_and_non_directional_criterion():
    assert "вопрос" in NoDataHypothesisOutput.model_fields["hypothesis"].description.lower()
    assert "установленному нарушению" in HypothesisSetOutput.model_json_schema()["description"]
    effect = Effect(affected="бизнес", metric="Охват нормы", how_to_calculate="Подсчитать затронутые случаи",
                    expected_trend="Проверка содержания нормы устанавливает дефект; охват только измеряет масштаб")
    assert effect.expected_trend.startswith("Проверка")
