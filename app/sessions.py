"""
Сессия работы с актом и её конец (2.7, мониторинг, шаг 3; решения владельца 13.10).

Сессия = одна загрузка акта = папка runs/<run_id>/: разбор, Excel, работа в чате. Конечная
точка — закрытие: кнопка «Завершить работу» на странице прогона или сама, если в журнале
сессии (events.jsonl) нет событий SESSION_IDLE_S секунд. Закрытая сессия навсегда —
открыть снова нельзя (решение владельца: позже, не сейчас).

При закрытии пишется событие session_closed и итог session_summary.json: сколько длилась,
сколько стоила (разбор и диалог), чем кончился разбор, сколько версий, что делал
пользователь, какие вызовы агентов и узлы не удались. Файл итога — признак закрытия:
журнал после него не пишется (trace.log_event), чат и правки отклоняет страница и сервер.

Сессия с идущим разбором («в очереди», «идёт») сама не закрывается. Прогоны до 2.7 без
журнала сами не закрываются — в их папки ничего не дописывается.
"""

from __future__ import annotations

import json
from datetime import datetime

from app import config, dialog, trace, versions
from app.contracts import RunMeta, RunProgress
from app.trace import (
    EVENTS_FILE, SESSION_SUMMARY_FILE, load_json, log_event, log_service_event, read_events, run_dir, save_json,
)

PROGRESS_FILE = "progress.json"   # то же имя, что в pipeline.PROGRESS_FILE (pipeline здесь не нужен целиком)
_FINISHED = ("готов", "сбой")     # разбор закончен — сессию можно закрыть


def is_closed(run_id: str) -> bool:
    """Закрыта ли сессия: есть итог session_summary.json."""
    return (run_dir(run_id) / SESSION_SUMMARY_FILE).is_file()


def last_activity(run_id: str) -> datetime | None:
    """Время последнего события журнала сессии; нет журнала — None. Читается только хвост файла."""
    path = run_dir(run_id) / EVENTS_FILE
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 16384))
        tail = f.read().decode("utf-8", errors="ignore").splitlines()
    for line in reversed(tail):
        try:
            return datetime.fromisoformat(json.loads(line)["ts"])
        except (ValueError, KeyError, TypeError):
            continue   # обрезанная первая строка хвоста
    return None


def _progress(run_id: str) -> RunProgress | None:
    try:
        return load_json(run_id, PROGRESS_FILE, RunProgress)
    except Exception:
        return None


def idle_sessions(now: datetime, idle_s: int = config.SESSION_IDLE_S) -> list[str]:
    """Открытые сессии с законченным разбором, в журнале которых нет событий дольше idle_s секунд."""
    found = []
    if not trace.RUNS_DIR.is_dir():   # через модуль trace — там же, где run_dir берёт папку прогонов
        return found
    for path in sorted(trace.RUNS_DIR.iterdir()):
        rid = path.name
        if not path.is_dir() or is_closed(rid):
            continue
        last = last_activity(rid)
        progress = _progress(rid)
        if last is None or progress is None or progress.status not in _FINISHED:
            continue
        if (now - last).total_seconds() > idle_s:
            found.append(rid)
    return found


def split_costs(events: list[dict], meta_cost: float | None, progress_cost: float | None,
                chat: list[dict]) -> dict[str, float]:
    """
    Стоимость сессии {"разбор", "диалог", "всего"} по журналу, meta.json, progress.json и чату — одна разбивка для
    итога сессии и отчёта scripts/session_report.py.
    15.10 (код-ревью): после применённой правки progress и meta уже содержат стоимость диалога — второй раз её не
    прибавляем. Разбор — стоимость на конец разбора (событие run_finished), всего — meta.json; без события (прогон
    до 2.7) — как раньше: progress + чат.
    """
    finished = next((e for e in reversed(events) if e.get("event") == "run_finished" and "cost_usd" in e), None)
    if finished is not None:
        run_cost = round(float(finished["cost_usd"] or 0), 4)
        total = round(meta_cost, 4) if meta_cost is not None else run_cost
        return {"разбор": run_cost, "диалог": round(max(total - run_cost, 0.0), 4), "всего": total}
    run_cost = round(progress_cost or 0.0, 4)
    dialog_cost = round(sum(float(e.get("cost_usd") or 0) for e in chat), 4)
    return {"разбор": run_cost, "диалог": dialog_cost, "всего": round(run_cost + dialog_cost, 4)}


def _summary(run_id: str, by: str, now: datetime) -> dict:
    """Итог сессии — из meta.json, progress.json, чата, версий и журнала."""
    events = read_events(run_id)
    progress = _progress(run_id)
    try:
        meta = load_json(run_id, "meta.json", RunMeta)
    except Exception:
        meta = None
    chat = dialog.read_chat(run_id)
    state = versions.load_state(run_id)
    started = meta.started_at if meta is not None else (events[0]["ts"] if events else None)
    costs = split_costs(events, meta.cost_usd if meta is not None else None,
                        progress.total_cost_usd if progress is not None else None, chat)
    roles: dict[str, int] = {}
    for e in chat:
        if e.get("role") in ("пользователь", "ассистент"):
            roles[e["role"]] = roles.get(e["role"], 0) + 1
    agents_done = [e for e in events if e["event"] == "agent_finished"]
    return {
        "run_id": run_id,
        "act_filename": meta.act_filename if meta is not None else None,
        "started_at": started,
        "closed_at": now.isoformat(timespec="seconds"),
        "closed_by": by,
        "duration_s": round((now - datetime.fromisoformat(started)).total_seconds()) if started else None,
        "last_activity": events[-1]["ts"] if events else None,
        "run_status": progress.status if progress is not None else None,
        "verdicts": progress.summary.model_dump(mode="json") if progress is not None and progress.summary else None,
        "cost_usd": costs,
        "version": state.current if state is not None else 0,
        "chat": roles,
        "user_actions": [{"ts": e["ts"], "action": e.get("action")} for e in events if e["event"] == "user_action"],
        "agents": {
            "вызовов": len(agents_done),
            "не удались": [e.get("trace") for e in agents_done if not e.get("ok")],
            "обрывов по таймауту": sum(1 for e in events if e["event"] == "agent_attempt"
                                      and e.get("outcome") == "timeout"),
        },
        "node_failures": [{"node": e.get("node"), "error": e.get("error")} for e in events
                          if e["event"] == "node_failed"],
    }


def close_session(run_id: str, *, by: str, now: datetime | None = None) -> bool:
    """
    Закрыть сессию: событие session_closed, затем итог session_summary.json (после него журнал
    заморожен). by — «пользователь» (кнопка) или «таймер» (простой). False — уже закрыта.
    """
    if is_closed(run_id):
        return False
    now = now or datetime.now()
    summary = _summary(run_id, by, now)
    log_event(run_id, "session_closed", by=by, cost_usd=summary["cost_usd"]["всего"], version=summary["version"])
    save_json(run_id, SESSION_SUMMARY_FILE, summary)
    log_service_event("session_closed", run_id=run_id, by=by)
    return True
