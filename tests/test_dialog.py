"""
Тесты app/dialog.py — один ход диалога с результатом прогона (план итерации 3, шаг 4).
runner.call подменяется: проверяется сборка входа, журнал чата, учёт денег.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import app.dialog as dialog_module
from app.contracts import DialogTurn, RunMeta, SetVerdict
from app.runner import CallResult
from app.trace import load_json, run_dir, save_json
from test_versions import _write_run


@pytest.fixture
def rid(tmp_runs_dir, tmp_path, monkeypatch) -> str:
    rid = "run_dialog"
    _write_run(rid)
    save_json(rid, "meta.json", RunMeta(run_id=rid, act_filename="act.docx", started_at="2026-09-07T09:00:00",
                                        finished_at="2026-09-07T09:15:00", status="finished", cost_usd=10.0))
    (run_dir(rid) / "01_document.md").write_text("ТЕКСТ АКТА: п. 2.4.1 ежедневный осмотр", encoding="utf-8")
    prompt = tmp_path / "07_dialog.md"
    prompt.write_text("Промпт 07_dialog (фикстура).", encoding="utf-8")
    monkeypatch.setattr(dialog_module, "PROMPT_DIALOG", prompt)
    return rid


def _ok(value: DialogTurn, cost: float = 0.4) -> CallResult:
    return CallResult(value=value, ok=True, attempts=1, cost_usd=cost, session_id="s", error=None,
                      permission_denials=[], duration_s=1.0)


def test_turn_sends_all_blocks_and_records_chat(rid, monkeypatch):
    seen = {}

    def _effect(instruction, document, model_cls, **kw):
        seen.update(instruction=instruction, document=document, model_cls=model_cls, **kw)
        return _ok(DialogTurn(reply="Индикатор 3 нарушен, потому что…"))

    monkeypatch.setattr(dialog_module, "call", Mock(side_effect=_effect))
    dialog_module.append_chat(rid, "сервис", "применено: версия 1")

    res = dialog_module.turn(rid, "Почему индикатор 3 нарушен?")

    doc = seen["document"]
    for block in ("## ПОЛНЫЙ ТЕКСТ АКТА", "## ВЕРДИКТЫ (JSON)", "## КАРТА ГИПОТЕЗ (JSON)", "## ЗАПРОС ПОКАЗАТЕЛЕЙ (JSON)",
                  "## СОСТОЯНИЕ ВЕРСИЙ (JSON)", "## ИСТОРИЯ ДИАЛОГА", "## СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ"):
        assert block in doc
    assert doc.rstrip().endswith("Почему индикатор 3 нарушен?")
    assert "применено: версия 1" in doc
    assert seen["model_cls"] is DialogTurn and seen["allowed_tools"] == []
    assert seen["model"] == dialog_module.DIALOG_MODEL
    assert seen["trace_name"].startswith("dialog/turns/") and seen["instruction"] == "Промпт 07_dialog (фикстура)."
    assert res.reply.startswith("Индикатор 3") and res.plan == []
    roles = [m["role"] for m in dialog_module.read_chat(rid)]
    assert roles == ["сервис", "пользователь", "ассистент"]
    assert load_json(rid, "meta.json", RunMeta).cost_usd == pytest.approx(10.4)


def test_turn_returns_plan(rid, monkeypatch):
    op = SetVerdict(kind="смена вердикта", indicator=7, verdict="нарушен", rationale="15 форм", reason="эксперт")
    monkeypatch.setattr(dialog_module, "call", Mock(return_value=_ok(DialogTurn(reply="Сменю вердикт.", plan=[op]))))
    res = dialog_module.turn(rid, "Эксперт: инд. 7 не соблюдён")
    assert res.plan == [op]
    assert dialog_module.read_chat(rid)[-1]["plan"][0]["kind"] == "смена вердикта"


def test_turn_failure_gives_readable_reply_without_plan(rid, monkeypatch):
    failed = CallResult(value=None, ok=False, attempts=3, cost_usd=0.1, session_id=None, error="таймаут",
                        permission_denials=[], duration_s=1.0)
    monkeypatch.setattr(dialog_module, "call", Mock(return_value=failed))
    res = dialog_module.turn(rid, "Что с индикатором 3?")
    assert res.plan == [] and "не получилось" in res.reply.lower()


def test_remarks_queue_one_by_one(rid):
    from app.contracts import Remark
    rs = [Remark(sheet="Вердикты", key="1", title="т", agree="нет"), Remark(sheet="Карта гипотез", key="2", title="г", text="x")]
    assert dialog_module.remarks_progress(rid) is None
    dialog_module.save_remarks(rid, "эксперт.xlsx", rs)
    assert dialog_module.remarks_progress(rid) == (0, 2)
    assert dialog_module.next_remark(rid) == rs[0]
    assert dialog_module.next_remark(rid) == rs[1]
    assert dialog_module.next_remark(rid) is None and dialog_module.remarks_progress(rid) == (2, 2)
