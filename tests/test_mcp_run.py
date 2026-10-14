"""Тесты app/mcp_run.py — инструмент помощника «прочитать артефакт прогона» (итерация 4, 14.10)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import mcp_run


@pytest.fixture
def run(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "run_x"
    (root / "03_verdicts").mkdir(parents=True)
    (root / "01_document.md").write_text("\n".join(f"строка {i}" for i in range(1, 701)) + "\n2.8.1. Отказ", encoding="utf-8")
    (root / "03_verdicts" / "03.json").write_text('{"verdict": "нарушен"}', encoding="utf-8")
    (root / "03_verdicts" / "03.checks.json").write_text('{"references": []}', encoding="utf-8")
    (root / "03_verdicts" / "03.raw.json").write_text("{}", encoding="utf-8")          # сырой ответ — не читается
    (root / "run.log").write_text("лог", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("секрет", encoding="utf-8")
    monkeypatch.setenv("P10_RUN_DIR", str(root))
    return root


def _call(name: str, **args) -> tuple[str, bool]:
    resp = mcp_run.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return resp["result"]["content"][0]["text"], resp["result"]["isError"]


def test_list_only_clean_artifacts(run):
    text, err = _call("list_artifacts")
    assert not err and text.split("\n") == ["01_document.md", "03_verdicts/03.checks.json", "03_verdicts/03.json"]


def test_read_by_chunks_and_query(run):
    text, err = _call("read_artifact", path="01_document.md")
    assert not err and text.startswith("1: строка 1") and "дальше — start_line=301" in text
    text, _ = _call("read_artifact", path="01_document.md", start_line=601)
    assert "601: строка 601" in text and "701: 2.8.1. Отказ" in text and "дальше" not in text
    text, _ = _call("read_artifact", path="01_document.md", query="2.8.1")
    assert text == "701: 2.8.1. Отказ"


@pytest.mark.parametrize("path", ["../secret.txt", "03_verdicts/03.raw.json", "run.log", "/etc/passwd", "06_map.json"])
def test_refuses_outside_raw_and_missing(run, path):
    text, err = _call("read_artifact", path=path)
    assert err and ("нельзя" in text or "вне папки" in text or "нет" in text)


def test_protocol_over_stdio(run):
    """Сервер целиком: initialize → уведомление → tools/list → tools/call, по строке JSON на сообщение."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_artifact",
                                                                         "arguments": {"path": "03_verdicts/03.json"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "нет_такого"},
    ]
    out = subprocess.run([sys.executable, "-m", "app.mcp_run"], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                         capture_output=True, text=True, timeout=30,
                         env={**__import__("os").environ, "P10_RUN_DIR": str(run)}, cwd=Path(mcp_run.__file__).parents[1])
    replies = [json.loads(line) for line in out.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4]                       # на уведомление ответа нет
    assert replies[0]["result"]["serverInfo"]["name"] == "p10run"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["list_artifacts", "read_artifact"]
    assert "нарушен" in replies[2]["result"]["content"][0]["text"]
    assert replies[3]["error"]["code"] == -32601
