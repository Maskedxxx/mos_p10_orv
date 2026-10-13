"""
Тесты журнала сессии runs/<run_id>/events.jsonl (2.7, мониторинг): одна строка — одно событие.

Журнал собирает в одном месте то, что раньше было разбросано по run.log, progress.json,
dialog/chat.jsonl и var/logs/actions.jsonl: создание сессии, записанные артефакты, вызовы
агентов, сообщения чата, версии, действия пользователя. События узлов пайплайна — в
test_pipeline.py, вызовы агентов — в test_runner.py.
"""

from __future__ import annotations

import json

from app import config, dialog
from app.actions import Who, log_action
from app.trace import EVENTS_FILE, log_event, new_run, read_events, run_dir, save_bytes, save_json, save_raw, save_text


def _events(run_id: str, kind: str | None = None) -> list[dict]:
    return [e for e in read_events(run_id) if kind is None or e["event"] == kind]


def test_log_event_appends_one_line_per_event(tmp_runs_dir):
    """Строка — JSON с временем, событием и тем, кто действует; файл только дописывается."""
    rid = new_run("акт.docx")
    log_event(rid, "проба", who="агент 03", n=1)
    log_event(rid, "проба", n=2)
    lines = (run_dir(rid) / EVENTS_FILE).read_text(encoding="utf-8").splitlines()
    probes = [json.loads(line) for line in lines if json.loads(line)["event"] == "проба"]
    assert [(p["who"], p["n"]) for p in probes] == [("агент 03", 1), ("система", 2)]
    assert all(p["ts"] for p in probes)


def test_log_event_never_breaks_the_caller(tmp_runs_dir):
    """Журнал — наблюдение, а не работа: нет папки прогона — событие не пишется, исключения нет."""
    log_event("нет_такого_прогона", "проба")
    assert not (tmp_runs_dir / "нет_такого_прогона").exists()


def test_new_run_opens_session_with_settings(tmp_runs_dir):
    """Первое событие сессии — её создание с выбором пользователя при загрузке."""
    rid = new_run("акт.docx", act_types=["господдержка"], comparison_regions=["Татарстан"], search_limits={3: 7})
    first = read_events(rid)[0]
    assert first["event"] == "session_created" and first["act_filename"] == "акт.docx"
    assert first["act_types"] == ["господдержка"] and first["comparison_regions"] == ["Татарстан"]
    assert first["search_limits"] == {"3": 7}


def test_artifacts_are_logged_with_size(tmp_runs_dir):
    """Каждый записанный артефакт — событие с путём и размером; повтор сырого ответа — под своим именем."""
    rid = new_run("акт.docx")
    save_text(rid, "01_document.md", "текст")
    save_json(rid, "03_verdicts/03.json", {"verdict": "нарушен"})
    save_bytes(rid, "00_input.docx", b"docx")
    save_raw(rid, "03_verdicts/03.raw.json", "[]")
    save_raw(rid, "03_verdicts/03.raw.json", "[]")
    paths = [e["path"] for e in _events(rid, "artifact")]
    assert paths == ["01_document.md", "03_verdicts/03.json", "00_input.docx",
                     "03_verdicts/03.raw.json", "03_verdicts/03.raw.attempt2.json"]
    assert _events(rid, "artifact")[0]["bytes"] == len("текст".encode("utf-8"))


def test_service_files_are_not_logged_as_artifacts(tmp_runs_dir):
    """meta.json, progress.json, статус диалога и снимки версий пишутся часто — в журнале был бы шум."""
    rid = new_run("акт.docx")
    save_json(rid, "progress.json", {})
    save_bytes(rid, "dialog/status.json", b"{}")
    save_bytes(rid, "versions/v1/06_map.json", b"{}")
    assert _events(rid, "artifact") == []


def test_chat_message_is_logged(tmp_runs_dir):
    """Реплика чата — событие с ролью и началом текста; полный текст — в dialog/chat.jsonl."""
    rid = new_run("акт.docx")
    dialog.append_chat(rid, "пользователь", "почему инд. 3 нарушен? " * 50, session="s-1")
    dialog.append_chat(rid, "ассистент", "потому что", plan=[{}, {}], cost_usd=0.01)
    user, bot = _events(rid, "chat")
    assert user["who"] == "пользователь" and user["session"] == "s-1"
    assert len(user["text"]) == 300 and user["text"].startswith("почему инд. 3")
    assert bot["who"] == "помощник" and bot["plan_steps"] == 2 and bot["cost_usd"] == 0.01


def test_user_action_on_a_run_goes_to_session_journal(tmp_runs_dir, tmp_path, monkeypatch):
    """Действие пользователя по прогону — и в общий журнал действий, и в журнал сессии; без прогона — только в общий."""
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    rid = new_run("акт.docx")
    who = Who(session="s-abc123", address="127.0.0.1")
    log_action(who, "скачивание Excel", rid, version=2)
    log_action(who, "вход")
    ev = _events(rid, "user_action")
    assert len(ev) == 1 and ev[0]["action"] == "скачивание Excel" and ev[0]["version"] == 2
    assert ev[0]["who"] == "пользователь" and ev[0]["session"] == "s-abc123"
    assert len((tmp_path / "logs" / "actions.jsonl").read_text(encoding="utf-8").splitlines()) == 2



def test_service_journal_appends_events():
    """Журнал сервиса var/logs/service.jsonl: событие — строка со временем; папка создаётся сама."""
    from app.trace import log_service_event, read_service_events
    log_service_event("service_started", port=8090)
    log_service_event("run_queued", run_id="r1", ahead=0)
    assert [(e["event"], e.get("port"), e.get("run_id")) for e in read_service_events()] == [
        ("service_started", 8090, None), ("run_queued", None, "r1")]
    assert all(e["ts"] for e in read_service_events())


def test_session_close_goes_to_service_journal(tmp_runs_dir):
    """Закрытие сессии видно и в журнале сервиса — какие сессии когда закрылись и как."""
    from app import sessions
    from app.trace import read_service_events
    rid = new_run("акт.docx")
    sessions.close_session(rid, by="таймер")
    closed = [e for e in read_service_events() if e["event"] == "session_closed"]
    assert closed == [{"ts": closed[0]["ts"], "event": "session_closed", "run_id": rid, "by": "таймер"}]
