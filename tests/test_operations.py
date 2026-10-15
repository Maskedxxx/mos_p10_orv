"""
Тесты app/operations.py — операции диалогового режима (план итерации 3, шаг 3).

Прогон — маленький синтетический (как в tests/test_versions.py); модель не
вызывается: узлы с моделью подменяются в модуле operations.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook

import app.operations as ops_module
from app import versions
from app.contracts import (
    AddMapRow, AppendRequest, BuildHypotheses, Buckets, DataItem, DataItemOutput, DataRequest, DeleteMapRow,
    DeleteRequestItem,
    EditText, Effect, Hypothesis, HypothesisMap, HypothesisSet, MarkNotTracked, MergeMapRows, RecheckIndicator,
    RequestItem, RunMeta,
    SetVerdict, SourceRef, SplitPart, SplitRequestItem, Verdict,
)
from app.operations import OperationError, accept, rollback, run_step
from app.trace import load_json, run_dir, save_json, update_meta
from test_versions import _write_run

_EFFECT = Effect(affected="бизнес", metric="издержки, руб.", how_to_calculate="сумма", expected_trend="рост")


@pytest.fixture
def rid(tmp_runs_dir) -> str:
    rid = "run_ops"
    _write_run(rid)
    save_json(rid, "meta.json", RunMeta(run_id=rid, act_filename="act.docx", started_at="2026-09-07T09:00:00",
                                        finished_at="2026-09-07T09:15:00", status="finished", cost_usd=10.0))
    (run_dir(rid) / "01_document.md").write_text("текст акта", encoding="utf-8")
    return rid


def _map(rid) -> HypothesisMap:
    return load_json(rid, "06_map.json", HypothesisMap)


def _req(rid) -> DataRequest:
    return load_json(rid, "07_request.json", DataRequest)


def test_edit_map_text_commits_version_with_journal_and_mark(rid):
    res = run_step(rid, EditText(kind="правка текста", sheet="строка карты", key="1", field="гипотеза",
                                  new_text="короче", reason="вода"))
    assert res.version == 1 and res.candidate is None
    assert _map(rid).rows[0].hypothesis_brief == ["короче"]          # 2.5: правится выжимка — её видит эксперт
    assert _map(rid).rows[0].hypothesis == load_json(rid, "versions/v0/06_map.json", HypothesisMap).rows[0].hypothesis
    assert versions.load_state(rid).marks.edited_rows == [1]
    wb = load_workbook(run_dir(rid) / "08_result.xlsx")
    assert wb.sheetnames[-1] == "Журнал изменений"
    assert [e.reason for e in versions.read_journal(rid)] == ["вода"]
    assert (run_dir(rid) / "versions" / "v1" / "06_map.json").exists()


def test_invalid_edit_is_refused_and_nothing_committed(rid):
    before = (run_dir(rid) / "07_request.json").read_bytes()
    with pytest.raises(OperationError):
        run_step(rid, EditText(kind="правка текста", sheet="позиция запроса", key="1", field="показатель",
                               new_text="количество и доля заявок", reason="x"))   # две величины в одной позиции
    assert versions.load_state(rid).current == 0
    assert (run_dir(rid) / "07_request.json").read_bytes() == before


def test_set_verdict_into_work_marks_indicator_without_rows(rid):
    run_step(rid, SetVerdict(kind="смена вердикта", indicator=5, verdict="нарушен", rationale="эксперт: пакет избыточен",
                             reason="эксперт"))
    buckets = load_json(rid, "04_buckets.json", Buckets)
    assert 5 in [v.indicator for v in buckets.violated]
    assert load_json(rid, "03_verdicts/05.json", Verdict).rationale == "эксперт: пакет избыточен"
    assert load_json(rid, "03_verdicts/05.json", Verdict).rationale_brief == ["эксперт: пакет избыточен"]
    marks = versions.load_state(rid).marks
    assert 5 in marks.edited_verdicts and 5 in marks.stale_verdicts


def test_set_verdict_out_of_work_marks_its_rows_stale(rid):
    run_step(rid, SetVerdict(kind="смена вердикта", indicator=3, verdict="соблюдён", rationale="эксперт", reason="эксперт"))
    assert set(versions.load_state(rid).marks.stale_rows) == {1, 2}


def test_delete_row_keeps_numbers_and_marks_orphan_item(rid):
    run_step(rid, DeleteMapRow(kind="удалить строку карты", row_id=1, reason="несостоятельна"))
    assert [r.row_id for r in _map(rid).rows] == [2]
    assert "1" in versions.load_state(rid).marks.stale_items          # позиция держалась только на строке 1
    run_step(rid, AddMapRow(kind="добавить строку карты", indicators=[10], hypothesis="h", grounds="эксперт",
                            effects=[_EFFECT], data=[DataItemOutput(metric="число вольеров", source="запрос органу")],
                            reason="эксперт"))
    assert [r.row_id for r in _map(rid).rows] == [2, 4]            # 1 не переиспользуется, 3 — «проверка не завершена»
    assert 4 in versions.load_state(rid).marks.stale_rows          # нет позиций запроса под новую строку


def test_merge_rows_rewires_request(rid):
    run_step(rid, MergeMapRows(kind="слить строки карты", row_ids=[1, 2], hypothesis="одна проблема", reason="дубль"))
    rows = _map(rid).rows
    assert [r.row_id for r in rows] == [1] and rows[0].hypothesis == "одна проблема"
    assert rows[0].hypothesis_brief == ["одна проблема"]
    assert _req(rid).items[0].for_rows == [1]


def test_split_item_takes_next_free_numbers(rid):
    run_step(rid, SplitRequestItem(kind="разделить позицию запроса", num="1", reason="две величины", parts=[
        SplitPart(metric="сумма (п. 1)", unit="тыс. руб."), SplitPart(metric="количество (п. 1)", unit="ед.")]))
    assert [it.num for it in _req(rid).items] == ["2", "3"]
    run_step(rid, DeleteRequestItem(kind="удалить позицию запроса", num="3", reason="не ведут"))
    assert [it.num for it in _req(rid).items] == ["2"]


def test_recheck_shows_candidate_and_writes_only_on_accept(rid, monkeypatch):
    new = Verdict(indicator=3, name="индикатор 3", verdict="соблюдён", rationale="всё действует")

    def _fake(document_md, run_id, **kw):
        assert kw["save"] is False and kw["extra_instruction"] == "проверь приказы Минтранса"
        update_meta(run_id, cost_usd=load_json(run_id, "meta.json", RunMeta).cost_usd + 0.5)
        return [new]

    monkeypatch.setattr(ops_module, "run_verdicts", _fake)
    res = run_step(rid, RecheckIndicator(kind="перепроверить индикатор", indicator=3,
                                         clarification="проверь приказы Минтранса", reason="эксперт"))
    assert res.version is None and res.candidate is not None
    assert res.candidate.cost_usd == pytest.approx(0.5)
    assert "нарушен" in res.candidate.before and "соблюдён" in res.candidate.after
    assert load_json(rid, "03_verdicts/03.json", Verdict).verdict == "нарушен"   # до «Принять» ничего не записано
    res = accept(rid, res.candidate)
    assert res.version == 1 and load_json(rid, "03_verdicts/03.json", Verdict).verdict == "соблюдён"
    assert versions.read_journal(rid)[0].cost_usd == pytest.approx(0.5)


def test_build_hypotheses_appends_rows_with_next_ids(rid, monkeypatch):
    run_step(rid, SetVerdict(kind="смена вердикта", indicator=7, verdict="нарушен", rationale="15 форм", reason="эксперт"))
    hset = HypothesisSet(indicator=7, name="индикатор 7", violation_kind="издержки", why_this_many="один механизм",
                         hypotheses=[Hypothesis(hypothesis="формы дублируют сведения", grounds="15 форм",
                                                effects=[_EFFECT], data=[DataItem(metric="число форм", source="запрос органу")],
                                                clause="8.3")])
    monkeypatch.setattr(ops_module, "run_hypotheses_violated", Mock(return_value=[hset]))
    res = run_step(rid, BuildHypotheses(kind="построить гипотезы", indicator=7, reason="вердикт стал «нарушен»"))
    res = accept(rid, res.candidate)
    rows = _map(rid).rows
    assert [r.row_id for r in rows] == [1, 2, 4] and rows[-1].indicators == [7]
    marks = versions.load_state(rid).marks
    assert 7 not in marks.stale_verdicts and 4 in marks.stale_rows


def test_build_hypotheses_refused_for_text_family_no_data(rid):
    with pytest.raises(OperationError):
        run_step(rid, BuildHypotheses(kind="построить гипотезы", indicator=4, reason="x"))   # «нет данных» по 1–6


def test_append_request_renumbers_from_state(rid, monkeypatch):
    run_step(rid, AddMapRow(kind="добавить строку карты", indicators=[10], hypothesis="h", grounds="эксперт",
                            effects=[_EFFECT], data=[DataItemOutput(metric="число вольеров", source="запрос органу")],
                            reason="эксперт"))
    existing = _req(rid)
    extended = DataRequest(items=existing.items + [RequestItem(
        num="2", kind="показатель по годам", metric="количество вольеров (п. 5.1)", unit="ед.", for_rows=[4],
        sources=[SourceRef(row_id=4, metric="число вольеров")])], years=existing.years)
    monkeypatch.setattr(ops_module, "build_request", Mock(return_value=extended))
    res = accept(rid, run_step(rid, AppendRequest(kind="дополнить запрос", row_ids=[4], reason="новая строка")).candidate)
    assert [it.num for it in _req(rid).items] == ["1", "2"] and res.version == 2
    assert 4 not in versions.load_state(rid).marks.stale_rows


def test_failure_mid_commit_restores_root(rid, monkeypatch):
    before = (run_dir(rid) / "06_map.json").read_bytes()
    monkeypatch.setattr(ops_module, "build_excel", Mock(side_effect=RuntimeError("диск")))
    with pytest.raises(RuntimeError):
        run_step(rid, EditText(kind="правка текста", sheet="строка карты", key="1", field="гипотеза",
                               new_text="короче", reason="вода"))
    assert (run_dir(rid) / "06_map.json").read_bytes() == before
    assert versions.load_state(rid).current == 0


def test_rollback_is_new_version_with_old_content_and_marks(rid):
    original = _map(rid).rows[0].hypothesis
    for text in ("короче", "ещё короче"):
        run_step(rid, EditText(kind="правка текста", sheet="строка карты", key="1", field="гипотеза",
                               new_text=text, reason="вода"))
    counters = versions.load_state(rid).next_row_id, versions.load_state(rid).next_item_num

    res = rollback(rid, 1)
    assert res.version == 3 and _map(rid).rows[0].hypothesis_brief == ["короче"]
    assert versions.load_state(rid).marks.edited_rows == [1]

    res = rollback(rid, 0)
    st = versions.load_state(rid)
    assert res.version == 4 and _map(rid).rows[0].hypothesis == original
    assert _map(rid).rows[0].hypothesis_brief is None                # как в исходной версии
    assert st.marks.edited_rows == [] and (st.next_row_id, st.next_item_num) == counters
    assert [e.kind for e in versions.read_journal(rid)][-2:] == ["откат", "откат"]
    assert (run_dir(rid) / "versions" / "v2" / "06_map.json").exists()   # ничего не стёрто
    with pytest.raises(OperationError):
        rollback(rid, 4)



def test_edit_verdict_text_changes_brief_points_not_full_rationale(rid):
    """2.5: эксперт правит то, что видит в Excel, — выжимку; полный разбор остаётся для анализа."""
    full = load_json(rid, "03_verdicts/01.json", Verdict).rationale
    res = run_step(rid, EditText(kind="правка текста", sheet="вердикт", key="1", field="обоснование",
                                  new_text="• пункт один (п. 2.1)\n- пункт два\n\n", reason="коротко"))
    v = load_json(rid, "03_verdicts/01.json", Verdict)
    assert v.rationale_brief == ["пункт один (п. 2.1)", "пункт два"] and v.rationale == full
    assert res.change.before == full, "выжимки не было — «было» показывает полный текст, который видел эксперт"


def test_edit_effect_calculation_changes_its_brief(rid):
    run_step(rid, EditText(kind="правка текста", sheet="строка карты", key="1", field="как считать", effect_index=1,
                           new_text="число заявок × 2 часа", reason="коротко"))
    effect = _map(rid).rows[0].effects[0]
    assert effect.how_to_calculate_brief == ["число заявок × 2 часа"] and effect.how_to_calculate != "число заявок × 2 часа"



# ---- 2.7 (итерация 4, Ж3): «нигде не ведётся» — пометка эксперта ----


def test_mark_not_tracked_changes_source_and_drops_item(rid):
    """Источник цифры — «нигде не ведётся»; позиция бланка, державшаяся только на ней, удаляется; журнал есть."""
    res = run_step(rid, MarkNotTracked(kind="пометить: нигде не ведётся", row_id=1, metric="п1",
                                       reason="такой статистики не ведут"))
    assert res.version == 1
    dp = _map(rid).rows[0].data[0]
    assert (dp.metric, dp.source, dp.where) == ("п1", "нигде не ведётся", None)
    assert "1" not in [it.num for it in _req(rid).items]
    assert 1 in versions.load_state(rid).marks.edited_rows
    j = versions.read_journal(rid)[-1]
    assert j.kind == "решение эксперта" and j.reason == "такой статистики не ведут"


def test_mark_not_tracked_keeps_mandatory_item(rid):
    """15.10 (код-ревью): позиция с обязательным показателем не удаляется вместе с цифрой — она остаётся без ссылки
    на строку карты (обязательные показатели в бланке всегда, решение Р4)."""
    req = _req(rid)
    save_json(rid, "07_request.json", req.model_copy(update={"items": [req.items[0].model_copy(update={"mandatory": ["ГП-1"]})]}))
    run_step(rid, MarkNotTracked(kind="пометить: нигде не ведётся", row_id=1, metric="п1", reason="не ведут"))
    items = _req(rid).items
    assert [it.num for it in items] == ["1"] and items[0].mandatory == ["ГП-1"]
    assert items[0].sources == [] and items[0].for_rows == []


def test_old_passport_does_not_block_operations(rid):
    """15.10 (код-ревью): паспорт прогона до итерации 4 («внешним лицам» при процедуре и контроле «есть» — так у трёх
    прогонов прода) не роняет правки диалога: это правило проверяет только свежий ответ модели."""
    from pathlib import Path
    old = json.loads((Path(__file__).parent / "fixtures" / "passport_before_iter4.json").read_text(encoding="utf-8"))
    save_json(rid, "02_passport.json", old)
    res = run_step(rid, EditText(kind="правка текста", sheet="строка карты", key="1", field="гипотеза",
                                  new_text="короче", reason="вода"))
    assert res.version == 1


def _failed_rows(rid) -> list[list]:
    ws = load_workbook(run_dir(rid) / "08_result.xlsx")["Ошибки прогона"]
    return [[c for c in row if c is not None] for row in ws.iter_rows(values_only=True)]


def test_expert_deleting_rows_is_not_a_run_error(rid):
    """15.10 (код-ревью): эксперт сам удалил все строки карты нарушенного индикатора — это его решение, не сбой
    прогона: на листе «Ошибки прогона» про гипотезы ничего нет."""
    run_step(rid, DeleteMapRow(kind="удалить строку карты", row_id=1, reason="несостоятельна"))
    run_step(rid, DeleteMapRow(kind="удалить строку карты", row_id=2, reason="несостоятельна"))
    assert not any("гипотезы по нему не построены" in str(c) for row in _failed_rows(rid) for c in row)


def test_pipeline_gap_stays_on_errors_sheet_after_edits(rid):
    """Сбой узла гипотез при разборе («нарушен» без строки карты уже в v0) остаётся на листе «Ошибки прогона»
    и после правок эксперта."""
    v5 = Verdict(indicator=5, name="индикатор 5", verdict="нарушен", rationale="нарушение без гипотез")
    save_json(rid, "03_verdicts/05.json", v5)
    buckets = load_json(rid, "04_buckets.json", Buckets)
    save_json(rid, "04_buckets.json", buckets.model_copy(update={
        "violated": buckets.violated + [v5], "checked": [v for v in buckets.checked if v.indicator != 5]}))
    run_step(rid, EditText(kind="правка текста", sheet="строка карты", key="1", field="гипотеза",
                            new_text="короче", reason="вода"))
    assert any("гипотезы по нему не построены" in str(c) for row in _failed_rows(rid) for c in row)


def test_mark_not_tracked_open_question_with_code_does_not_block(rid):
    """15.10 (повторное ревью): открытый вопрос без строки карты невозможен — даже с обязательным кодом он уходит
    вместе с цифрой, а операция не отклоняется."""
    item = RequestItem(num="1", kind="открытый вопрос", metric="Какие основания отказа чаще всего? (п. 1)",
                       clause="п. 1", for_rows=[1], sources=[SourceRef(row_id=1, metric="п1")], mandatory=["ГП-1"])
    save_json(rid, "07_request.json", _req(rid).model_copy(update={"items": [item]}))
    run_step(rid, MarkNotTracked(kind="пометить: нигде не ведётся", row_id=1, metric="п1", reason="не ведут"))
    assert _req(rid).items == []


def test_expert_built_then_deleted_rows_not_a_run_error(rid, monkeypatch):
    """15.10 (повторное ревью): сбой гипотез при разборе эксперт закрыл сам («построить гипотезы»), потом удалил эти
    строки — это уже его решение, на листе «Ошибки прогона» индикатора нет."""
    v5 = Verdict(indicator=5, name="индикатор 5", verdict="нарушен", rationale="нарушение без гипотез")
    save_json(rid, "03_verdicts/05.json", v5)
    buckets = load_json(rid, "04_buckets.json", Buckets)
    save_json(rid, "04_buckets.json", buckets.model_copy(update={
        "violated": buckets.violated + [v5], "checked": [v for v in buckets.checked if v.indicator != 5]}))
    hset = HypothesisSet(indicator=5, name="индикатор 5", violation_kind="издержки", why_this_many="один механизм",
                         hypotheses=[Hypothesis(hypothesis="отказ без исправления", grounds="п. 5",
                                                effects=[_EFFECT], data=[DataItem(metric="число отказов",
                                                                                  source="запрос органу")],
                                                clause="5")])
    monkeypatch.setattr(ops_module, "run_hypotheses_violated", Mock(return_value=[hset]))
    accept(rid, run_step(rid, BuildHypotheses(kind="построить гипотезы", indicator=5, reason="эксперт")).candidate)
    new_row = next(r.row_id for r in _map(rid).rows if r.indicators == [5])
    run_step(rid, DeleteMapRow(kind="удалить строку карты", row_id=new_row, reason="несостоятельна"))
    assert not any("гипотезы по нему не построены" in str(c) for row in _failed_rows(rid) for c in row)


def test_mark_not_tracked_unknown_metric_refused(rid):
    with pytest.raises(OperationError):
        run_step(rid, MarkNotTracked(kind="пометить: нигде не ведётся", row_id=1, metric="нет такой", reason="x"))
    assert versions.load_state(rid).current == 0


def test_model_cannot_add_row_with_not_tracked():
    """Ж3: в «добавить строку карты» модель диалога пометку эксперта поставить не может."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AddMapRow(kind="добавить строку карты", indicators=[10], hypothesis="h", grounds="э", effects=[_EFFECT],
                  data=[{"metric": "x", "source": "нигде не ведётся"}], reason="э")


def test_recheck_accept_puts_new_checks_table_to_root(rid, monkeypatch):
    """14.10: «Принять» перепроверки кладёт в корень и новую таблицу проверки — помощник читает её, а не старую."""
    new = Verdict(indicator=3, name="индикатор 3", verdict="соблюдён", rationale="всё действует")
    (run_dir(rid) / "03_verdicts" / "03.checks.json").write_text('{"references": ["старая"]}', encoding="utf-8")

    def _fake(document_md, run_id, **kw):
        path = run_dir(run_id) / f"{kw['trace_prefix']}03_verdicts/03.checks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"references": ["новая"]}', encoding="utf-8")
        return [new]

    monkeypatch.setattr(ops_module, "run_verdicts", _fake)
    res = run_step(rid, RecheckIndicator(kind="перепроверить индикатор", indicator=3, clarification="по таблице",
                                         reason="эксперт"))
    assert '"старая"' in (run_dir(rid) / "03_verdicts" / "03.checks.json").read_text(encoding="utf-8")   # до «Принять»
    accept(rid, res.candidate)
    assert '"новая"' in (run_dir(rid) / "03_verdicts" / "03.checks.json").read_text(encoding="utf-8")
