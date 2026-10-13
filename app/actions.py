"""
Журнал действий на странице сервиса П-10.

Бета: один логин на всех, поэтому люди различаются по сессии браузера — короткой
метке из id, который NiceGUI хранит в cookie браузера (s-7f3a91). Каждое действие —
одна строка var/logs/actions.jsonl: время, сессия, адрес, действие, прогон,
подробности. Файл только дописывается; сбой записи страницу не роняет — пишется
в лог сервиса.

Что происходит внутри прогона и диалога, пишут сами прогон (run.log, сырые ответы)
и диалог (dialog/chat.jsonl, versions/journal.jsonl); здесь — кто и когда нажал.
"""

from __future__ import annotations

import datetime
import json
import threading
from dataclasses import dataclass

from app import config
from app.logger import app_logger
from app.trace import log_event

ACTIONS_FILE = "actions.jsonl"
_lock = threading.Lock()   # страница одна на процесс, обработчики и фоновые действия пишут из разных потоков


@dataclass(frozen=True)
class Who:
    """Кто действует: метка сессии браузера и адрес, снятые при открытии страницы."""

    session: str
    address: str | None = None
    forwarded: str | None = None   # заголовок X-Forwarded-For — если пришли через туннель


def session_label(browser_id: str | None) -> str:
    """Короткая метка сессии браузера: первые шесть знаков его id."""
    return "s-" + browser_id.replace("-", "")[:6] if browser_id else "s-?"


def log_action(who: Who, action: str, run_id: str | None = None, **details) -> None:
    """Дописать действие в журнал; пустые поля не пишутся."""
    entry = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "session": who.session,
             "address": who.address, "forwarded": who.forwarded, "action": action, "run_id": run_id, **details}
    line = json.dumps({k: v for k, v in entry.items() if v is not None}, ensure_ascii=False)
    try:
        with _lock:
            config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
            with open(config.LOGS_DIR / ACTIONS_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        app_logger.exception("журнал действий: не записано «%s» (%s)", action, who.session)
    # Действие по прогону — ещё и в журнал его сессии (events.jsonl): там вся картина по акту
    if run_id:
        log_event(run_id, "user_action", who="пользователь", action=action, session=who.session,
                  **{k: v for k, v in details.items() if v is not None})
