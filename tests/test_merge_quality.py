from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.contracts import Hypothesis, HypothesisMap, MergeOutput
from app.input_contexts import merge_input
from app.nodes.merge import _candidate_pairs


def hyp(label="A", clause="п. 1", group="бизнес", metric="Часы"):
    return Hypothesis(hypothesis=f"Механизм {label}", grounds=f"Основание {label}", clause=clause,
                      proof=f"Цитата {label}", hypothesis_brief=[f"Кратко {label}"], grounds_brief=[f"Опора {label}"],
                      effects=[dict(affected=group, metric=metric, how_to_calculate=f"Метод {label}",
                                    expected_trend=f"Критерий {label}", how_to_calculate_brief=[f"Расчёт {label}"],
                                    expected_trend_brief=[f"Проверка {label}"])],
                      data=[dict(metric=f"Число {label}", source="запрос органу", origin_request_ids=[label])])


def inputs(*hypotheses):
    return [(i, i, hypothesis) for i, hypothesis in enumerate(hypotheses, 1)]


def context(flat):
    return {"merge_inputs": merge_input(flat, []).model_dump(mode="json")["hypotheses"]}


def row(number, source):
    i, n, h = source
    return h.model_dump(mode="json") | {"row_id": number, "indicators": [n]}


def preserved_rows(flat, decisions=None):
    return dict(rows=[row(i, item) for i, item in enumerate(flat, 1)], merges=[], decisions=decisions or [])


def full_merge(flat):
    payload = row(1, flat[0])
    payload.update(indicators=sorted({n for _, n, _ in flat}), hypothesis="Общая формулировка",
                   hypothesis_brief=["Общая краткая формулировка"], grounds_brief=["Общие основания"],
                   grounds=" | ".join(dict.fromkeys(h.grounds for _, _, h in flat)),
                   proof="; ".join(dict.fromkeys(h.proof for _, _, h in flat if h.proof)) or None,
                   clause="; ".join(dict.fromkeys(h.clause for _, _, h in flat if h.clause)) or None,
                   effects=[e.model_dump(mode="json") for _, _, h in flat for e in h.effects],
                   data=[d.model_dump(mode="json") for _, _, h in flat for d in h.data])
    return dict(rows=[payload], merges=[dict(merged_row_ids=[i for i, _, _ in flat], reason="Одно действие и один механизм")],
                decisions=[dict(row_ids=[flat[0][0], flat[1][0]], merge=True, reason="Один механизм")])


def test_different_clause_notation_does_not_forbid_full_merge():
    flat = inputs(hyp("A", "п. 1"), hyp("B", "пункт первый", group="город"))
    assert _candidate_pairs(flat) == []
    result = MergeOutput.model_validate(full_merge(flat), context=context(flat))
    assert result.rows[0].indicators == [1, 2]
    assert len(result.rows[0].effects) == 2


def test_different_units_and_methods_for_same_side_survive():
    flat = inputs(hyp("A", metric="Часы за год"), hyp("B", metric="Рубли за обращение"))
    result = MergeOutput.model_validate(full_merge(flat), context=context(flat))
    assert [e.metric for e in result.rows[0].effects] == ["Часы за год", "Рубли за обращение"]


def test_same_clause_different_mechanisms_and_uncertain_pair_may_stay_separate():
    flat = inputs(hyp("A"), hyp("B"))
    for reason in ("Разные механизмы одной нормы", "Недостаточно оснований отождествить механизмы"):
        result = MergeOutput.model_validate(preserved_rows(flat, [dict(row_ids=[1, 2], merge=False, reason=reason)]), context=context(flat))
        assert len(result.rows) == 2


def test_partial_overlap_outside_candidates_keeps_rows_unchanged():
    flat = inputs(hyp("A", "п. 1"), hyp("B", "п. 2"))
    decision = dict(row_ids=[1, 2], merge=False, reason="Одна операция общая, остальные различаются",
                    overlap="Получение одного согласования", accounting_note="Не складывать затраты на общую операцию без раздельного учёта")
    result = MergeOutput.model_validate(preserved_rows(flat, [decision]), context=context(flat))
    assert result.decisions[0].overlap == decision["overlap"]
    assert [r.hypothesis for r in result.rows] == [h.hypothesis for _, _, h in flat]


@pytest.mark.parametrize("change", [dict(accounting_note=None), dict(merge=True), dict(overlap=" ")])
def test_partial_overlap_requires_conservative_decision_and_accounting_note(change):
    flat = inputs(hyp("A"), hyp("B"))
    decision = dict(row_ids=[1, 2], merge=False, reason="Часть общая", overlap="Операция", accounting_note="Не учитывать дважды") | change
    with pytest.raises(ValidationError):
        MergeOutput.model_validate(preserved_rows(flat, [decision]), context=context(flat))


@pytest.mark.parametrize("field", ["hypothesis", "grounds", "proof", "clause", "hypothesis_brief"])
def test_nonmerged_rows_cannot_be_rewritten_even_when_count_is_correct(field):
    flat = inputs(hyp("A"), hyp("B"))
    payload = preserved_rows(flat)
    payload["rows"][0][field] = ["Другая выжимка"] if field.endswith("_brief") else "Подменённый текст"
    with pytest.raises(ValidationError, match="неслитая"):
        MergeOutput.model_validate(payload, context=context(flat))


@pytest.mark.parametrize("field", ["effects", "data", "grounds", "proof", "clause", "indicators"])
def test_full_merge_cannot_drop_source_fields(field):
    flat = inputs(hyp("A"), hyp("B", group="город"))
    payload = full_merge(flat)
    if isinstance(payload["rows"][0][field], list):
        payload["rows"][0][field] = payload["rows"][0][field][:1]
    else:
        payload["rows"][0][field] = "Неполное значение"
    with pytest.raises(ValidationError):
        MergeOutput.model_validate(payload, context=context(flat))


def test_exact_same_data_can_union_origin_ids_without_losing_them():
    a, b = hyp("A"), hyp("B", group="город")
    b.data[0].metric = a.data[0].metric
    flat = inputs(a, b)
    payload = full_merge(flat)
    payload["rows"][0]["data"] = [payload["rows"][0]["data"][0] | {"origin_request_ids": ["A", "B"]}]
    assert MergeOutput.model_validate(payload, context=context(flat))
    payload["rows"][0]["data"][0]["origin_request_ids"] = ["A"]
    with pytest.raises(ValidationError, match="origin_request_ids"):
        MergeOutput.model_validate(payload, context=context(flat))


def test_unknown_or_repeated_input_ids_are_rejected():
    flat = inputs(hyp("A"), hyp("B", group="город"))
    for ids in ([1, 1], [1, 99]):
        payload = full_merge(flat)
        payload["merges"][0]["merged_row_ids"] = ids
        payload["decisions"] = []
        with pytest.raises(ValidationError):
            MergeOutput.model_validate(payload, context=context(flat))
    payload = preserved_rows(flat, [dict(row_ids=[1, 99], merge=False, reason="Нет оснований")])
    with pytest.raises(ValidationError, match="неизвестная"):
        MergeOutput.model_validate(payload, context=context(flat))


def test_missing_candidate_is_still_diagnostic_not_fabricated_decision():
    flat = inputs(hyp("A"), hyp("B"))
    assert _candidate_pairs(flat)
    result = MergeOutput.model_validate(preserved_rows(flat), context=context(flat))
    assert result.decisions == []


def test_old_inputs_without_briefs_can_get_briefs_without_rewriting_full_fields():
    h = hyp("A")
    h.hypothesis_brief = h.grounds_brief = None
    h.effects[0].how_to_calculate_brief = h.effects[0].expected_trend_brief = None
    flat = inputs(h)
    payload = preserved_rows(flat)
    payload["rows"][0].update(hypothesis_brief=["Краткая формулировка"], grounds_brief=["Основание"])
    payload["rows"][0]["effects"][0].update(how_to_calculate_brief=["Метод"], expected_trend_brief=["Проверка"])
    assert MergeOutput.model_validate(payload, context=context(flat))


def test_archive_map_does_not_need_fresh_context_or_overlap_fields():
    data = preserved_rows(inputs(hyp("A"), hyp("B")), [dict(row_ids=[1, 2], merge=False, reason="Разные")])
    result = HypothesisMap.model_validate(data)
    assert result.decisions[0].overlap is None and result.decisions[0].accounting_note is None


def test_exact_duplicate_effects_are_not_counted_twice_in_full_merge():
    a, b = hyp("A"), hyp("B")
    b.effects = deepcopy(a.effects)
    flat = inputs(a, b)
    payload = full_merge(flat)
    with pytest.raises(ValidationError, match="точная копия"):
        MergeOutput.model_validate(payload, context=context(flat))
    payload["rows"][0]["effects"] = payload["rows"][0]["effects"][:1]
    assert MergeOutput.model_validate(payload, context=context(flat))
