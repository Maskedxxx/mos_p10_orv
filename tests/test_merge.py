"""
Тесты app/nodes/merge.py (узел [5], слияние карты гипотез одним вызовом модели).

runner.call подменяется моком — реально прогоняется только логика узла
(нумерация гипотез, сборка stdin, обработка провала без потери гипотез,
проверка сохранности после слияния, трейсы, прибавление стоимости). Промпт
05_merge.md подменяется фикстурным файлом во временном каталоге — каталог
prompts/ проекта не трогаем.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import app.nodes.merge as merge_module
from app.contracts import (
    DataItem,
    Effect,
    Hypothesis,
    HypothesisMap,
    HypothesisSet,
    MapRow,
    Merge,
    RunMeta,
)
from app.nodes.merge import _candidate_pairs, merge_map
from app.runner import CallResult
from app.trace import load_json, run_dir, save_json


def _hypothesis(text: str) -> Hypothesis:
    return Hypothesis(
        hypothesis=text,
        affected="субъекты МСП",
        effect=Effect(metric="число обращений", how_to_calculate="сумма за год"),
        data=[DataItem(metric="число заявок", source="запрос органу")],
    )


def _hset(indicator: int, texts: list[str]) -> HypothesisSet:
    return HypothesisSet(
        indicator=indicator,
        name=f"индикатор {indicator}",
        why_this_many=f"выявлено {len(texts)}",
        hypotheses=[_hypothesis(t) for t in texts],
    )


@pytest.fixture
def tmp_prompt(tmp_path, monkeypatch):
    """Подменить PROMPT_MERGE на фиктивный файл во временном каталоге."""
    path = tmp_path / "05_merge.md"
    path.write_text("Промпт 05_merge (фикстура).", encoding="utf-8")
    monkeypatch.setattr(merge_module, "PROMPT_MERGE", path)
    return path


def _make_run(rid: str, *, cost_usd: float = 0.0) -> None:
    meta = RunMeta(
        run_id=rid, act_filename="act.docx",
        started_at="2026-01-01T12:00:00", status="started", cost_usd=cost_usd,
    )
    save_json(rid, "meta.json", meta)


@pytest.fixture
def run_id(tmp_runs_dir) -> str:
    rid = "run_merge_test"
    _make_run(rid)
    return rid


def _rows_from(flat_texts: list[tuple[int, str]]) -> list[MapRow]:
    """Собрать список MapRow «как из входа» — по одной строке на гипотезу, без слияний."""
    return [
        MapRow(
            row_id=i,
            indicators=[indicator],
            hypothesis=text,
            effect=Effect(metric="число обращений", how_to_calculate="сумма за год"),
            data=[DataItem(metric="число заявок", source="запрос органу")],
        )
        for i, (indicator, text) in enumerate(flat_texts, start=1)
    ]


def _ok_call_result(value: HypothesisMap, *, cost_usd: float = 0.05) -> CallResult[HypothesisMap]:
    return CallResult(
        value=value, ok=True, attempts=1, cost_usd=cost_usd, session_id="s",
        error=None, permission_denials=[], duration_s=1.0,
    )


def _failed_call_result(*, cost_usd: float = 0.02) -> CallResult[HypothesisMap]:
    return CallResult(
        value=None, ok=False, attempts=3, cost_usd=cost_usd, session_id=None,
        error="процесс упал 3 раза", permission_denials=[], duration_s=1.0,
    )


def test_single_call_not_several(run_id, tmp_prompt, monkeypatch):
    """1. Один вызов модели, а не параллельный прогон по гипотезам."""
    hsets = [_hset(1, ["A", "B"]), _hset(7, ["C"])]
    result_map = HypothesisMap(rows=_rows_from([(1, "A"), (1, "B"), (7, "C")]), merges=[])
    mock_call = Mock(return_value=_ok_call_result(result_map))
    monkeypatch.setattr(merge_module, "call", mock_call)

    merge_map(hsets, "текст акта", run_id)

    assert mock_call.call_count == 1


def test_hypotheses_numbered_before_sending(run_id, tmp_prompt, monkeypatch):
    """2. Гипотезы пронумерованы сквозной нумерацией с 1 перед подачей в stdin."""
    hsets = [_hset(1, ["A", "B"]), _hset(7, ["C"])]
    result_map = HypothesisMap(rows=_rows_from([(1, "A"), (1, "B"), (7, "C")]), merges=[])
    seen_document = {}

    def _effect(instruction, document, model_cls, **kwargs):
        seen_document["doc"] = document
        return _ok_call_result(result_map)

    monkeypatch.setattr(merge_module, "call", Mock(side_effect=_effect))

    merge_map(hsets, "текст акта", run_id)

    doc = seen_document["doc"]
    assert "## ПОЛНЫЙ ТЕКСТ АКТА" in doc
    assert "текст акта" in doc
    assert "## ПРОНУМЕРОВАННЫЕ ГИПОТЕЗЫ (JSON)" in doc
    assert '"row_id": 1' in doc
    assert '"row_id": 2' in doc
    assert '"row_id": 3' in doc
    assert '"indicator": 1' in doc
    assert '"indicator": 7' in doc


def test_model_failure_gives_map_from_input_without_loss(run_id, tmp_prompt, monkeypatch):
    """3. Провал модели -> карта из входа без потерь, merges пустой."""
    hsets = [_hset(1, ["A", "B"]), _hset(7, ["C"])]
    monkeypatch.setattr(merge_module, "call", Mock(return_value=_failed_call_result()))

    hyp_map = merge_map(hsets, "текст акта", run_id)

    assert len(hyp_map.rows) == 3
    assert hyp_map.merges == []
    assert [r.hypothesis for r in hyp_map.rows] == ["A", "B", "C"]
    assert [r.indicators for r in hyp_map.rows] == [[1], [1], [7]]


def test_lost_hypothesis_is_caught_and_logged(run_id, tmp_prompt, monkeypatch):
    """4. Модель молча выбросила строку (без объявления слияния) -> ошибка в лог."""
    hsets = [_hset(1, ["A", "B", "C"])]
    # На входе 3 гипотезы, модель вернула только 2 строки без единого merge — потеря.
    broken_map = HypothesisMap(rows=_rows_from([(1, "A"), (1, "B")]), merges=[])
    monkeypatch.setattr(merge_module, "call", Mock(return_value=_ok_call_result(broken_map)))

    merge_map(hsets, "текст акта", run_id)

    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "ERROR" in log_text
    assert "расхождение в счёте строк" in log_text
    assert "вход=3" in log_text
    assert "выход=2" in log_text


def test_correct_merge_does_not_log_error(run_id, tmp_prompt, monkeypatch):
    """4b. Честное слияние (с объявленным Merge) счёт сходится -> ошибки в логе нет."""
    hsets = [_hset(1, ["A", "B", "C"])]
    merged_row = _rows_from([(1, "A и B слиты")])[0]
    ok_map = HypothesisMap(
        rows=[merged_row, *_rows_from([(1, "C")])],
        merges=[Merge(merged_row_ids=[1, 2], reason="одно и то же требование")],
    )
    monkeypatch.setattr(merge_module, "call", Mock(return_value=_ok_call_result(ok_map)))

    merge_map(hsets, "текст акта", run_id)

    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "расхождение в счёте строк" not in log_text


def test_artifacts_written(run_id, tmp_prompt, monkeypatch):
    """5. Артефакт 06_map.json создан с итоговой картой."""
    hsets = [_hset(1, ["A"])]
    result_map = HypothesisMap(rows=_rows_from([(1, "A")]), merges=[])
    monkeypatch.setattr(merge_module, "call", Mock(return_value=_ok_call_result(result_map)))

    merge_map(hsets, "текст акта", run_id)

    saved = load_json(run_id, "06_map.json", HypothesisMap)
    assert saved.rows[0].hypothesis == "A"


def test_cost_added_not_overwritten(tmp_runs_dir, tmp_prompt, monkeypatch):
    """6. Стоимость ПРИБАВЛЯЕТСЯ к уже записанной в meta.json, а не затирает её."""
    rid = "run_merge_cost_test"
    _make_run(rid, cost_usd=2.0)  # имитация суммы, уже записанной предыдущими узлами

    hsets = [_hset(1, ["A"])]
    result_map = HypothesisMap(rows=_rows_from([(1, "A")]), merges=[])
    monkeypatch.setattr(
        merge_module, "call", Mock(return_value=_ok_call_result(result_map, cost_usd=0.15)),
    )

    merge_map(hsets, "текст акта", rid)

    meta = load_json(rid, "meta.json", RunMeta)
    assert meta.cost_usd == pytest.approx(2.0 + 0.15)


def test_no_tools_and_prompt_used(run_id, tmp_prompt, monkeypatch):
    """7. Инструменты пустые, используется промпт 05_merge.md."""
    hsets = [_hset(1, ["A"])]
    result_map = HypothesisMap(rows=_rows_from([(1, "A")]), merges=[])
    seen = {}

    def _effect(instruction, document, model_cls, **kwargs):
        seen["instruction"] = instruction
        seen["tools"] = kwargs["allowed_tools"]
        seen["trace_name"] = kwargs["trace_name"]
        return _ok_call_result(result_map)

    monkeypatch.setattr(merge_module, "call", Mock(side_effect=_effect))

    merge_map(hsets, "текст акта", run_id)

    assert seen["tools"] == []
    assert "Промпт 05_merge" in seen["instruction"]
    assert seen["trace_name"] == "06_map.raw.json"


def test_empty_input_skips_model_call(run_id, tmp_prompt, monkeypatch):
    """Пустой вход (нет гипотез) -> модель не вызывается, пустая карта сохраняется."""
    mock_call = Mock()
    monkeypatch.setattr(merge_module, "call", mock_call)

    hyp_map = merge_map([], "текст акта", run_id)

    assert hyp_map.rows == []
    assert hyp_map.merges == []
    mock_call.assert_not_called()


# ---- ОС экспертов 2026-09-04, узел 5, находки В2/В4: кандидаты на слияние ----


def _hyp_c(text: str, clause: str | None) -> Hypothesis:
    """Гипотеза с заданным пунктом акта — для тестов кандидатов."""
    h = _hypothesis(text)
    return h.model_copy(update={"clause": clause})


def _hset_c(indicator: int, items: list[tuple[str, str | None]]) -> HypothesisSet:
    return HypothesisSet(
        indicator=indicator, name=f"индикатор {indicator}", why_this_many="тест",
        hypotheses=[_hyp_c(t, c) for t, c in items],
    )


def test_candidate_pairs_by_shared_clause():
    """В4. Пары гипотез с общим номером пункта акта — кандидаты на слияние.
    Случай 1112: инд. 10 «8» и инд. 11 «7, 8» — общий пункт 8, узел его
    пропустил «на глаз»; теперь пара обязана попасть в кандидаты."""
    flat = merge_module._flatten_hypotheses([
        _hset_c(10, [("сроки могут не соблюдаться", "8"), ("отказы как барьер", "10")]),
        _hset_c(11, [("сроки не соблюдаются на практике", "7, 8")]),
    ])
    pairs = _candidate_pairs(flat)
    assert [(a, b) for a, b, _ in pairs] == [(1, 3)]
    assert pairs[0][2] == ["8"]


def test_candidate_pairs_ignore_annex_numbers_and_missing_clause():
    """В4. «приложение 1» — не пункт: две гипотезы про разные пункты одного приложения
    кандидатами не становятся; гипотеза без пункта — тоже."""
    flat = merge_module._flatten_hypotheses([
        _hset_c(4, [("а", "1.3 (п. 2.6 приложения 1 к постановлению)"),
                    ("б", "1.11 (п. 7.1.6.3 приложения 1 к постановлению)"),
                    ("в", None)]),
    ])
    assert _candidate_pairs(flat) == []


def test_candidate_pairs_within_one_indicator_too():
    """В3. Дубли внутри одного индикатора — тоже кандидаты (ГРАНТЫ инд. 6, обе п. 1.4)."""
    flat = merge_module._flatten_hypotheses([_hset_c(6, [("а", "1.4"), ("б", "1.4")])])
    assert [(a, b) for a, b, _ in _candidate_pairs(flat)] == [(1, 2)]


def test_candidates_block_sent_to_model(run_id, tmp_prompt, monkeypatch):
    """В4. Кандидаты уходят модели отдельным помеченным JSON-блоком после гипотез."""
    seen = {}

    def _effect(instruction, document, schema, **kwargs):
        seen["doc"] = document
        return _ok_call_result(HypothesisMap(rows=_rows_from([(10, "A"), (11, "B")]), merges=[]))

    monkeypatch.setattr(merge_module, "call", Mock(side_effect=_effect))
    merge_map([_hset_c(10, [("A", "8")]), _hset_c(11, [("B", "7, 8")])], "текст акта", run_id)
    doc = seen["doc"]
    assert "## КАНДИДАТЫ НА СЛИЯНИЕ (JSON)" in doc
    assert doc.index("## ПРОНУМЕРОВАННЫЕ ГИПОТЕЗЫ (JSON)") < doc.index("## КАНДИДАТЫ НА СЛИЯНИЕ (JSON)")
    assert '"row_ids": [\n      1,\n      2\n    ]' in doc or '"row_ids": [1, 2]' in doc
    assert '"shared_clauses"' in doc


def test_candidate_without_decision_is_logged_as_error(run_id, tmp_prompt, monkeypatch):
    """В2/В4. Модель не дала решения по кандидату — это и есть «пропуск на глаз»
    (1112 инд. 10↔11). Фиксируется в логе как ERROR, как и потеря строки."""
    result_map = HypothesisMap(rows=_rows_from([(10, "A"), (11, "B")]), merges=[], decisions=[])
    monkeypatch.setattr(merge_module, "call", Mock(return_value=_ok_call_result(result_map)))
    merge_map([_hset_c(10, [("A", "8")]), _hset_c(11, [("B", "7, 8")])], "текст акта", run_id)
    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "ERROR" in log_text
    assert "кандидат без решения" in log_text
    assert "[1, 2]" in log_text


def test_decision_for_every_candidate_no_error(run_id, tmp_prompt, monkeypatch):
    """В4, граница: по каждому кандидату есть решение — ошибок в логе нет."""
    result_map = HypothesisMap(
        rows=_rows_from([(10, "A"), (11, "B")]), merges=[],
        decisions=[{"row_ids": [1, 2], "merge": False, "reason": "механизмы разные"}],
    )
    monkeypatch.setattr(merge_module, "call", Mock(return_value=_ok_call_result(result_map)))
    merge_map([_hset_c(10, [("A", "8")]), _hset_c(11, [("B", "7, 8")])], "текст акта", run_id)
    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "кандидат без решения" not in log_text
