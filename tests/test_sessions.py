"""
Тесты app/sessions.py — конец сессии (2.7, мониторинг, шаг 3).

Сессия = одна загрузка акта = папка runs/<run_id>/. Закрывается кнопкой «Завершить работу»
или сама через час без событий в журнале; закрытая сессия навсегда: итог в
session_summary.json, журнал больше не пишется, чат и правки отклоняются.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from app import dialog, sessions
from app.contracts import NodeState, RunProgress, RunSummary
from app.trace import EVENTS_FILE, log_event, new_run, read_events, run_dir, save_json

_NOW = datetime(2026, 10, 13, 12, 0, 0)


def _finished_run(status: str = "готов", cost: float = 4.8) -> str:
    """Прогон с завершённым разбором: progress.json со сводкой, журнал с событиями."""
    rid = new_run("акт.docx")
    save_json(rid, "progress.json", RunProgress(
        run_id=rid, act_filename="акт.docx", status=status, nodes=[NodeState(key="excel", title="Excel")],
        started_at="2026-10-13T10:00:00", finished_at="2026-10-13T10:08:00", total_cost_usd=cost,
        summary=RunSummary(violated=5, compliant=4, not_applicable=2, no_data=1, errors=0, map_rows=10,
                           merges=1, request_items=34),
    ))
    return rid


def _age_journal(rid: str, minutes: int) -> None:
    """Сдвинуть время всех событий журнала на minutes минут назад от _NOW."""
    path = run_dir(rid) / EVENTS_FILE
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for e in lines:
        e["ts"] = (_NOW - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in lines), encoding="utf-8")


def test_close_writes_summary_and_event(tmp_runs_dir):
    """Закрытие: событие session_closed и итог сессии; повторное закрытие ничего не меняет."""
    rid = _finished_run()
    dialog.append_chat(rid, "пользователь", "почему 3?")
    dialog.append_chat(rid, "ассистент", "потому что", cost_usd=0.12)
    log_event(rid, "user_action", who="пользователь", action="скачивание Excel")
    assert not sessions.is_closed(rid)
    assert sessions.close_session(rid, by="пользователь", now=_NOW) is True
    assert sessions.is_closed(rid)
    summary = json.loads((run_dir(rid) / sessions.SESSION_SUMMARY_FILE).read_text(encoding="utf-8"))
    assert summary["closed_by"] == "пользователь" and summary["closed_at"].startswith("2026-10-13T12:00")
    assert summary["run_status"] == "готов" and summary["verdicts"]["violated"] == 5
    assert summary["cost_usd"] == {"разбор": 4.8, "диалог": 0.12, "всего": 4.92}
    assert summary["chat"] == {"пользователь": 1, "ассистент": 1}
    assert summary["user_actions"][0]["action"] == "скачивание Excel"
    assert summary["version"] == 0
    closed = [e for e in read_events(rid) if e["event"] == "session_closed"]
    assert len(closed) == 1 and closed[0]["by"] == "пользователь"
    assert sessions.close_session(rid, by="таймер", now=_NOW) is False
    assert json.loads((run_dir(rid) / sessions.SESSION_SUMMARY_FILE).read_text(encoding="utf-8"))["closed_by"] == "пользователь"


def test_summary_counts_agent_failures(tmp_runs_dir):
    """В итоге — сколько было вызовов агентов, какие не удались и сколько попыток оборвалось по таймауту."""
    rid = _finished_run()
    log_event(rid, "agent_finished", trace="03_verdicts/01.raw.json", ok=True)
    log_event(rid, "agent_attempt", trace="03_verdicts/03.raw.json", outcome="timeout")
    log_event(rid, "agent_finished", trace="03_verdicts/03.raw.json", ok=False, error="таймаут")
    log_event(rid, "node_failed", node="sort", error="сбой")
    sessions.close_session(rid, by="пользователь", now=_NOW)
    summary = json.loads((run_dir(rid) / sessions.SESSION_SUMMARY_FILE).read_text(encoding="utf-8"))
    assert summary["agents"] == {"вызовов": 2, "не удались": ["03_verdicts/03.raw.json"], "обрывов по таймауту": 1}
    assert summary["node_failures"] == [{"node": "sort", "error": "сбой"}]


def test_closed_session_journal_is_frozen(tmp_runs_dir):
    """После закрытия журнал не пишется: папка сессии больше не меняется."""
    rid = _finished_run()
    sessions.close_session(rid, by="пользователь", now=_NOW)
    before = (run_dir(rid) / EVENTS_FILE).read_text(encoding="utf-8")
    log_event(rid, "user_action", action="скачивание Excel")
    assert (run_dir(rid) / EVENTS_FILE).read_text(encoding="utf-8") == before


def test_idle_sessions_after_an_hour_without_events(tmp_runs_dir):
    """Час без событий в журнале — сессия к закрытию; 59 минут — нет."""
    old = _finished_run()
    fresh = _finished_run()
    _age_journal(old, 61)
    _age_journal(fresh, 59)
    assert sessions.idle_sessions(_NOW, idle_s=3600) == [old]


def test_idle_sessions_skip_running_closed_and_old_runs(tmp_runs_dir):
    """Не закрываются: разбор ещё идёт или в очереди; уже закрытые; прогоны до 2.7 без журнала. Сбойный — закрывается."""
    running = _finished_run(status="идёт")
    failed = _finished_run(status="сбой")
    closed = _finished_run()
    sessions.close_session(closed, by="пользователь", now=_NOW - timedelta(hours=3))
    legacy = _finished_run()
    (run_dir(legacy) / EVENTS_FILE).unlink()
    for rid in (running, failed, closed):
        _age_journal(rid, 120)
    assert sessions.idle_sessions(_NOW, idle_s=3600) == [failed]


def test_last_activity_is_last_event(tmp_runs_dir):
    """Последняя активность — время последнего события журнала."""
    rid = _finished_run()
    _age_journal(rid, 30)
    assert sessions.last_activity(rid) == _NOW - timedelta(minutes=30)
    assert sessions.last_activity("нет_такого") is None


def test_dialog_cost_not_counted_twice(tmp_runs_dir):
    """15.10 (код-ревью): после применённой правки progress и meta уже содержат стоимость ходов помощника — итог
    сессии не прибавляет её второй раз: «разбор» — по событию конца разбора, «всего» — по meta.json."""
    from app.contracts import RunMeta
    rid = _finished_run(cost=13.0)                      # progress после правки = meta: разбор 10 + диалог 3
    save_json(rid, "meta.json", RunMeta(run_id=rid, act_filename="акт.docx", started_at="2026-10-13T10:00:00",
                                        finished_at="2026-10-13T10:08:00", status="finished", cost_usd=13.0))
    log_event(rid, "run_finished", status="готов", cost_usd=10.0)
    dialog.append_chat(rid, "ассистент", "ответ", cost_usd=3.0)
    sessions.close_session(rid, by="пользователь", now=_NOW)
    summary = json.loads((run_dir(rid) / sessions.SESSION_SUMMARY_FILE).read_text(encoding="utf-8"))
    assert summary["cost_usd"] == {"разбор": 10.0, "диалог": 3.0, "всего": 13.0}
