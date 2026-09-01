"""
Тесты app/nodes/summary.py (шаг 3, узел [1.1]).

runner.call_text подменяется моком — реально прогоняется только логика узла
(чтение промпта, вызов, сохранение, обработка ok=False). Живой тест (маркер
`live`) дёргает настоящую модель и по умолчанию не запускается.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import Mock

import pytest

import app.nodes.summary as summary_module
from app.nodes.convert import convert_to_markdown
from app.nodes.summary import make_summary
from app.runner import CallResult
from app.trace import run_dir

FIXTURES = Path(__file__).parent / "fixtures"


def _ok_result(value: str) -> CallResult[str]:
    return CallResult(
        value=value, ok=True, attempts=1, cost_usd=0.01, session_id="s1",
        error=None, permission_denials=[], duration_s=1.0,
    )


def _failed_result(error: str) -> CallResult[str]:
    return CallResult(
        value=None, ok=False, attempts=3, cost_usd=0.03, session_id=None,
        error=error, permission_denials=[], duration_s=3.0,
    )


def test_make_summary_saves_artifact_and_returns_text(tmp_runs_dir, monkeypatch):
    """1. Мок call_text -> узел сохраняет 02_summary.md и возвращает текст."""
    mock_call_text = Mock(return_value=_ok_result("## Саммари\n\nТекст саммари приказа."))
    monkeypatch.setattr(summary_module, "call_text", mock_call_text)

    result = make_summary("текст акта в markdown", run_id="run_summary_ok")

    assert result == "## Саммари\n\nТекст саммари приказа."
    saved_path = run_dir("run_summary_ok") / "02_summary.md"
    assert saved_path.exists()
    assert saved_path.read_text(encoding="utf-8") == result

    # Документ ушёл в stdin вторым позиционным аргументом, инструментов не дано
    args = mock_call_text.call_args.args
    assert args[1] == "текст акта в markdown"
    assert mock_call_text.call_args.kwargs["allowed_tools"] == []


def test_make_summary_raises_on_ok_false(tmp_runs_dir, monkeypatch):
    """2. ok=False от runner -> узел бросает исключение."""
    mock_call_text = Mock(return_value=_failed_result("процесс упал 3 раза"))
    monkeypatch.setattr(summary_module, "call_text", mock_call_text)

    with pytest.raises(RuntimeError):
        make_summary("текст акта", run_id="run_summary_fail")


def test_make_summary_reads_real_prompt_and_passes_nonblank_instruction(tmp_runs_dir, monkeypatch):
    """3. Промпт реально читается из файла и уходит в вызов: аргумент инструкции
    непустой и содержит характерный кусок реального промпта (prompts/01_summary.md)."""
    mock_call_text = Mock(return_value=_ok_result("саммари"))
    monkeypatch.setattr(summary_module, "call_text", mock_call_text)

    make_summary("текст акта", run_id="run_summary_prompt")

    instruction = mock_call_text.call_args.args[0]
    assert instruction.strip() != ""
    # характерный кусок раздела 5 промпта "Ключевые цифры"
    assert "Ключевые цифры" in instruction


# ---- Живой тест ----

_CLAUSE_REFERENCE_RE = re.compile(r"(п\.|пункт)\s*\d+(\.\d+)*", re.IGNORECASE)


@pytest.mark.live
def test_live_summary_of_dsmi_order(tmp_runs_dir):
    """4. Живое саммари приказа ДСМИ: номер приказа 02-02-313/21 и хотя бы одна
    ссылка на пункт (промпт требует выписывать цифры с указанием пункта)."""
    document_md = convert_to_markdown(FIXTURES / "prikaz_dsmi.docx", run_id="run_live_summary")

    summary = make_summary(document_md, run_id="run_live_summary")

    assert "02-02-313/21" in summary
    assert _CLAUSE_REFERENCE_RE.search(summary) is not None
