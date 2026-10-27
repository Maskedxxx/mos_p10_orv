"""
Тесты долгих действий чата, замка правки и конца сессии (app/dialog_jobs.py; 27.10 перенесены из tests/test_web.py
вместе с кодом).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from app import config, dialog_jobs, trace, web
from app.actions import Who
from app.contracts import (
    AppendRequest, BuildHypotheses, ChatEntry, DeleteMapRow, EditText, Marks, RecheckIndicator, RunMeta, RunProgress,
    SetVerdict,
)
from app.operations import Change, StepResult


def test_dialog_lock_one_editor_until_release_or_timeout():
    lock = dialog_jobs.DialogLock(ttl_s=10)
    assert lock.acquire("r", "a", now=0)
    assert not lock.acquire("r", "b", now=5)
    assert lock.acquire("r", "a", now=8)        # окно-хозяин продлевает
    assert not lock.acquire("r", "b", now=15)
    assert lock.acquire("r", "b", now=19)       # хозяин пропал дольше ttl — правка переходит
    lock.release("r", "a")                      # чужой замок не снимается
    assert not lock.acquire("r", "a", now=20)
    lock.release("r", "b")
    assert lock.acquire("r", "a", now=21)
    assert lock.acquire("другой", "b", now=21)  # замок — на прогон, не на сервис


def test_op_title_is_human_readable():
    t = dialog_jobs.op_title(SetVerdict(kind="смена вердикта", indicator=7, verdict="нарушен", rationale="15 форм", reason="э"))
    assert t == "Сменить вердикт индикатора 7 на «нарушен»: 15 форм"
    t = dialog_jobs.op_title(EditText(kind="правка текста", sheet="строка карты", key="3", field="гипотеза",
                              new_text="коротко", reason="вода"))
    assert t == "Поправить текст — строка карты 3, поле «гипотеза»: «коротко»"
    assert dialog_jobs.op_title(DeleteMapRow(kind="удалить строку карты", row_id=4, reason="x")) == "Удалить строку карты 4"
    assert "агентом запроса" in dialog_jobs.op_title(AppendRequest(kind="дополнить запрос", row_ids=[8, 9], reason="x"))


def test_op_title_for_every_operation_kind():
    """15.10 (код-ревью): у каждого вида операции диалога есть подпись — иначе карточка плана падает и шаг из
    интерфейса не применить (так было с «пометить: нигде не ведётся»). Новый вид без образца здесь — тест красный."""
    import typing
    from app.contracts import (AddMapRow, DataItemOutput, DeleteRequestItem, Effect, MarkNotTracked, MergeMapRows,
                               Operation, SplitPart, SplitRequestItem)
    effect = Effect(affected="бизнес", metric="издержки, руб.", how_to_calculate="сумма", expected_trend="рост")
    samples = [
        EditText(kind="правка текста", sheet="строка карты", key="3", field="гипотеза", new_text="коротко", reason="x"),
        SetVerdict(kind="смена вердикта", indicator=7, verdict="нарушен", rationale="15 форм", reason="x"),
        DeleteMapRow(kind="удалить строку карты", row_id=4, reason="x"),
        AddMapRow(kind="добавить строку карты", indicators=[10], hypothesis="h", grounds="эксперт", effects=[effect],
                  data=[DataItemOutput(metric="число вольеров", source="запрос органу")], reason="x"),
        MergeMapRows(kind="слить строки карты", row_ids=[1, 2], hypothesis="одна проблема", reason="x"),
        MarkNotTracked(kind="пометить: нигде не ведётся", row_id=2, metric="число жалоб", reason="x"),
        DeleteRequestItem(kind="удалить позицию запроса", num="5", reason="x"),
        SplitRequestItem(kind="разделить позицию запроса", num="5", reason="x", parts=[
            SplitPart(metric="сумма (п. 1)", unit="тыс. руб."), SplitPart(metric="количество (п. 1)", unit="ед.")]),
        RecheckIndicator(kind="перепроверить индикатор", indicator=3, clarification="проверь приказы", reason="x"),
        BuildHypotheses(kind="построить гипотезы", indicator=7, reason="x"),
        AppendRequest(kind="дополнить запрос", row_ids=[8, 9], reason="x"),
    ]
    kinds = {typing.get_args(m.model_fields["kind"].annotation)[0] for m in typing.get_args(Operation)}
    assert {op.kind for op in samples} == kinds
    assert all(dialog_jobs.op_title(op) for op in samples)
    title = dialog_jobs.op_title(samples[5])
    assert "строка карты 2" in title and "число жалоб" in title and "нигде не ведётся" in title


def _entry(**fields) -> ChatEntry:
    """Реплика чата для теста (27.10: чат — контракт ChatEntry, время в тестах не важно)."""
    return ChatEntry(at="2026-09-25T13:00:00", **fields)


def test_pending_plan_only_right_after_assistant():
    op = SetVerdict(kind="смена вердикта", indicator=7, verdict="нарушен", rationale="15 форм", reason="э")
    chat = [_entry(role="пользователь", text="?"), _entry(role="ассистент", text="сменю", plan=[op.model_dump(mode="json")])]
    assert dialog_jobs.pending_plan(chat) == [op]
    assert dialog_jobs.pending_plan(chat + [_entry(role="сервис", text="План отменён")]) == []
    assert dialog_jobs.pending_plan([_entry(role="ассистент", text="ответ", plan=[])]) == []


def test_stale_notes_show_only_new_marks():
    before = Marks(stale_rows={1: "старое"})
    after = Marks(stale_rows={1: "старое", 2: "вердикт инд. 7 изменён"}, stale_verdicts={7: "гипотез нет"})
    assert dialog_jobs.stale_notes(before, after) == ["Вердикты, инд. 7: гипотез нет", "строка карты 2: вердикт инд. 7 изменён"]


def test_step_text_short_and_long():
    short = StepResult(version=3, change=Change("Вердикты, инд. 7", "вердикт изменён", "соблюдён", "нарушен"))
    assert dialog_jobs.step_text(short) == "Версия 3. Вердикты, инд. 7 — вердикт изменён: «соблюдён» → «нарушен»"
    long = StepResult(version=4, change=Change("Карта гипотез, стр. 1", "текст поправлен", "а" * 90, "б" * 90))
    assert dialog_jobs.step_text(long).splitlines()[1:] == ["Было: " + "а" * 90, "Стало: " + "б" * 90]


def test_plan_summary_texts():
    assert dialog_jobs.plan_summary(2, 2, 0, None, None, 3) == (
        "Готово: план применён (2 из 2 шагов). Файл — версия 3, скачать можно кнопкой вверху.", "готово")
    text, mark = dialog_jobs.plan_summary(3, 2, 1, None, None, 5)
    assert mark == "готово" and "(2 из 3 шагов, отклонено 1)" in text
    assert dialog_jobs.plan_summary(1, 0, 1, None, None, 2) == ("План завершён без изменений — файл остался в версии 2.", "отменено")
    assert dialog_jobs.plan_summary(2, 1, 0, 2, "диск", 4) == (
        "План применён частично: 1 из 2 шагов, файл — версия 4. Шаг 2 не выполнен — диск.", "частично")
    assert dialog_jobs.plan_summary(2, 0, 0, 1, "строки 9 нет", 4) == (
        "План не применён: шаг 1 не выполнен — строки 9 нет. Файл не изменён (версия 4).", "сбой")


def _wait(predicate):
    async def waiter():
        for _ in range(300):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("не дождались")
    return waiter()


def test_dialog_jobs_plan_waits_for_decision_on_server(tmp_runs_dir, monkeypatch):
    from app import dialog
    from app.operations import Candidate
    from test_versions import _write_run
    rid = "20260925_130000_ffffff"
    _write_run(rid)
    edit = EditText(kind="правка текста", sheet="строка карты", key="1", field="гипотеза", new_text="к", reason="в")
    recheck = RecheckIndicator(kind="перепроверить индикатор", indicator=3, clarification="c", reason="r")
    cand = Candidate(recheck, None, "соблюдён — старое", "нарушен — новое", 1.5)
    monkeypatch.setattr(web.operations, "run_step", lambda run_id, op: (
        StepResult(version=None, candidate=cand, cost_usd=1.5) if op is recheck
        else StepResult(version=1, change=Change("Карта гипотез, стр. 1", "переформулировано", "а", "к"))))
    monkeypatch.setattr(web.operations, "accept", lambda run_id, c: StepResult(
        version=2, change=Change("Вердикты, инд. 3", "перепроверен", "соблюдён", "нарушен"), cost_usd=1.5))

    async def scenario():
        jobs = dialog_jobs.DialogJobs()
        assert jobs.start(rid, jobs.apply_plan(rid, [edit, recheck], session="s-aaaaaa"))
        await _wait(lambda: jobs.waiting(rid) is not None)
        title, shown = jobs.waiting(rid)
        assert shown is cand and title.startswith("Шаг 2 из 2")
        assert dialog.read_status(rid).state == "решение"
        assert not jobs.start(rid, jobs.turn(rid, "ещё"))   # по прогону уже идёт действие
        jobs.decide(rid, True)
        await _wait(lambda: not jobs.busy(rid))

    asyncio.run(scenario())
    chat = dialog.read_chat(rid)
    assert [m.mark for m in chat] == [None, "шаг", "шаг", "готово"]
    assert {m.session for m in chat} == {"s-aaaaaa"}   # кто применял план — у каждой строки
    assert chat[-1].text.startswith("Готово: план применён (2 из 2 шагов)")
    assert sum(m.cost_usd or 0.0 for m in chat) == pytest.approx(1.5)   # стоимость записана (на экран не идёт)
    assert dialog.read_status(rid) is None


def test_dialog_jobs_failure_stops_plan_with_reason(tmp_runs_dir, monkeypatch):
    from app import dialog
    from test_versions import _write_run
    rid = "20260925_130000_gggggg"
    _write_run(rid)
    edit = EditText(kind="правка текста", sheet="строка карты", key="9", field="гипотеза", new_text="к", reason="в")

    def boom(run_id, op):
        raise web.operations.OperationError("строки 9 нет")

    monkeypatch.setattr(web.operations, "run_step", boom)

    async def scenario():
        jobs = dialog_jobs.DialogJobs()
        jobs.start(rid, jobs.apply_plan(rid, [edit, edit]))
        await _wait(lambda: not jobs.busy(rid))

    asyncio.run(scenario())
    chat = dialog.read_chat(rid)
    assert [m.mark for m in chat] == [None, "сбой"]
    assert chat[-1].text == "План не применён: шаг 1 не выполнен — строки 9 нет. Файл не изменён (версия 0)."
    assert dialog.read_status(rid) is None


def _ready_run(rid: str, status: str = "готов") -> RunProgress:
    from test_versions import _write_run
    _write_run(rid)
    progress = RunProgress.model_validate({"run_id": rid, "act_filename": "a.docx", "status": status,
                                           "started_at": "2026-10-13T10:00:00", "nodes": []})
    trace.save_json(rid, "progress.json", progress)
    trace.save_json(rid, "meta.json", RunMeta(run_id=rid, act_filename="a.docx", started_at="2026-10-13T10:00:00",
                                              status="finished"))
    return progress


def test_dialog_jobs_reopen_closed_session(tmp_runs_dir):
    """23.10: первое действие в чате открывает закрытую сессию заново — итог прошлой в sessions/01.json."""
    from app import sessions
    rid = "20261013_100000_bbbbbb"
    _ready_run(rid)
    sessions.close_session(rid, by="пользователь")

    async def job():
        return None

    async def scenario():
        jobs = dialog_jobs.DialogJobs()
        assert jobs.start(rid, job()) is True

    asyncio.run(scenario())
    assert not sessions.is_closed(rid)
    assert (trace.run_dir(rid) / "sessions" / "01.json").is_file()


def test_close_by_user(tmp_runs_dir, tmp_path, monkeypatch):
    """Кнопка «Завершить работу»: нельзя, пока идёт разбор или действие чата; иначе — действие в журнал и закрытие."""
    from types import SimpleNamespace
    from app import sessions
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    who = Who(session="s-aaaaaa")
    running = "20261013_100000_cccccc"
    _ready_run(running, status="идёт")
    assert "Разбор ещё идёт" in dialog_jobs.close_by_user(who, running, SimpleNamespace(busy=lambda r: False))
    rid = "20261013_100000_dddddd"
    _ready_run(rid)
    assert "действие в чате" in dialog_jobs.close_by_user(who, rid, SimpleNamespace(busy=lambda r: True))
    assert not sessions.is_closed(rid)
    assert dialog_jobs.close_by_user(who, rid, SimpleNamespace(busy=lambda r: False)) is None
    assert sessions.is_closed(rid)
    events = trace.read_events(rid)
    assert [e["event"] for e in events[-2:]] == ["user_action", "session_closed"]
    assert events[-2]["action"] == "завершить работу" and events[-1]["by"] == "пользователь"
    assert dialog_jobs.close_by_user(who, rid, SimpleNamespace(busy=lambda r: False)) == "Сессия уже закрыта."


def test_close_idle_sessions_skips_busy_dialog(tmp_runs_dir, monkeypatch):
    """Фоновое закрытие по простою: сессия, по которой идёт действие диалога, не закрывается."""
    from types import SimpleNamespace
    from app import sessions
    a, b = "20261013_100000_eeeeee", "20261013_100000_ffffff"
    _ready_run(a)
    _ready_run(b)
    monkeypatch.setattr(sessions, "idle_sessions", lambda now: [a, b])
    closed = dialog_jobs.close_idle_sessions(datetime(2026, 10, 13, 12, 0), SimpleNamespace(busy=lambda r: r == b))
    assert closed == [a] and sessions.is_closed(a) and not sessions.is_closed(b)
    assert json.loads((trace.run_dir(a) / sessions.SESSION_SUMMARY_FILE).read_text(encoding="utf-8"))["closed_by"] == "таймер"
