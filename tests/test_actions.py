"""
Тесты app/actions.py — журнал действий на странице (бета: один логин на всех,
сессии браузеров различаются меткой).
"""

from __future__ import annotations

import json

import pytest

from app import config
from app.actions import Who, log_action, session_label


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    return tmp_path / "logs"


def _lines(logs_dir):
    return [json.loads(l) for l in (logs_dir / "actions.jsonl").read_text(encoding="utf-8").splitlines()]


def test_session_label_is_short_and_stable():
    assert session_label("7f3a91c2-0b1d-4e5f-9a8b-123456789abc") == "s-7f3a91"
    assert session_label("7f3a91c2-0b1d-4e5f-9a8b-123456789abc") == session_label("7f3a91c2-ffff")
    assert session_label(None) == "s-?"


def test_log_action_appends_one_line_per_action(logs_dir):
    who = Who("s-7f3a91", "10.0.0.5", "203.0.113.7")
    log_action(who, "открытие прогона", run_id="r1")
    log_action(Who("s-bbbbbb"), "вход")
    first, second = _lines(logs_dir)
    assert first["session"] == "s-7f3a91" and first["action"] == "открытие прогона" and first["run_id"] == "r1"
    assert first["address"] == "10.0.0.5" and first["forwarded"] == "203.0.113.7" and first["at"]
    assert second == {"at": second["at"], "session": "s-bbbbbb", "action": "вход"}   # пустое не пишется


def test_log_action_details_are_kept(logs_dir):
    log_action(Who("s-aaaaaa"), "скачивание Excel", run_id="r1", version=3, file="МАДИ — карта гипотез, версия 3.xlsx")
    (entry,) = _lines(logs_dir)
    assert entry["version"] == 3 and entry["file"].endswith("версия 3.xlsx")


def test_log_action_never_breaks_the_page(tmp_path, monkeypatch):
    blocker = tmp_path / "занято"
    blocker.write_text("не папка", encoding="utf-8")
    monkeypatch.setattr(config, "LOGS_DIR", blocker)   # папку создать нельзя
    log_action(Who("s-aaaaaa"), "вход")                 # не падает
