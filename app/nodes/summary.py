"""
Узел [1.1] пайплайна П-10 — саммари приказа (шаг 1.1).

Зовёт модель в текстовом режиме (runner.call_text): промпт из
prompts/01_summary.md + текст акта в stdin, без инструментов (саммари
делается по тексту, в интернет ходить не нужно). Результат сохраняется в
трейс как 02_summary.md; сырой ответ модели сохраняет сам runner под именем
02_summary.raw.json.
"""

from __future__ import annotations

from app.config import PROMPTS_DIR
from app.runner import call_text
from app.trace import save_text


def make_summary(document_md: str, run_id: str) -> str:
    """
    Сделать саммари приказа: промпт из prompts/01_summary.md + текст акта в stdin.

    Бросает RuntimeError, если runner вернул ok=False — саммари нужно дальше
    по пайплайну, без него идти некуда.
    """
    instruction = (PROMPTS_DIR / "01_summary.md").read_text(encoding="utf-8")

    result = call_text(
        instruction,
        document_md,
        run_id=run_id,
        trace_name="02_summary.raw.json",
        allowed_tools=[],
    )

    if not result.ok:
        raise RuntimeError(f"не удалось получить саммари приказа: {result.error}")
    assert result.value is not None  # инвариант CallResult: ok=True <=> value задан

    save_text(run_id, "02_summary.md", result.value)
    return result.value
