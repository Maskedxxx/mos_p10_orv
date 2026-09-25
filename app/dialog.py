"""
Один ход диалога с результатом прогона (план итерации 3, шаг 4).

Оркестратор — ещё один вызов runner.call по контракту DialogTurn: ответ
пользователю и план из закрытого списка операций. Сам ничего не меняет —
план выполняет app/operations.py после подтверждения пользователя.

Каждый ход без памяти CLI: вход собирается заново из текущей версии прогона
(текст акта, паспорт, вердикты, карта, запрос, состояние версий) и истории
чата из dialog/chat.jsonl. Так у модели не остаётся данных, прочитанных
до применённой правки. Всё идёт через stdin, инструментов нет — у агента
нет доступа ни к файлам, ни к сети (шаг 0 плана: чтение папкой прогона
на спарке надёжно не ограничить).
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field

from app import versions
from app.config import ALLOWED_TOOLS_TEXT, DIALOG_MODEL, PROMPT_DIALOG
from app.contracts import DialogTurn, Operation
from app.nodes.hypotheses import _add_cost_to_meta
from app.runner import call
from app.trace import run_dir

CHAT = "dialog/chat.jsonl"
HISTORY_MESSAGES = 16       # сколько последних реплик чата уходит модели
_ROLES = ("пользователь", "ассистент", "сервис")


@dataclass
class TurnResult:
    reply: str
    plan: list[Operation] = field(default_factory=list)
    cost_usd: float = 0.0


def read_chat(run_id: str) -> list[dict]:
    path = run_dir(run_id) / CHAT
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def append_chat(run_id: str, role: str, text: str, **extra) -> dict:
    """Дописать реплику в чат прогона: пользователь, ассистент или сервис (что применено, что отклонено)."""
    assert role in _ROLES, role
    entry = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "role": role, "text": text, **extra}
    path = run_dir(run_id) / CHAT
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _json_block(title: str, raw: str) -> str:
    return f"## {title}\n\n```json\n{raw.strip()}\n```\n"


def build_document(run_id: str, message: str, history: list[dict]) -> str:
    """Вход модели: текущая версия прогона, история чата, сообщение пользователя — последним блоком."""
    root = run_dir(run_id)
    verdicts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((root / "03_verdicts").glob("[0-9][0-9].json"))]
    for v in verdicts:
        v.pop("attempts", None)
    state = versions.load_state(run_id)
    parts = [f"## ПОЛНЫЙ ТЕКСТ АКТА\n\n{(root / '01_document.md').read_text(encoding='utf-8')}\n"]
    if (root / "02_passport.json").is_file():
        parts.append(_json_block("ПАСПОРТ АКТА (JSON)", (root / "02_passport.json").read_text(encoding="utf-8")))
    parts += [
        _json_block("ВЕРДИКТЫ (JSON)", json.dumps(verdicts, ensure_ascii=False, indent=2)),
        _json_block("КАРТА ГИПОТЕЗ (JSON)", (root / "06_map.json").read_text(encoding="utf-8")),
        _json_block("ЗАПРОС ПОКАЗАТЕЛЕЙ (JSON)", (root / "07_request.json").read_text(encoding="utf-8")),
        _json_block("СОСТОЯНИЕ ВЕРСИЙ (JSON)", state.model_dump_json(indent=2) if state else "{}"),
    ]
    lines = [f"{m['role']}: {m['text']}" for m in history] or ["(начало диалога)"]
    parts.append("## ИСТОРИЯ ДИАЛОГА\n\n" + "\n\n".join(lines) + "\n")
    parts.append(f"## СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ\n\n{message}\n")
    return "\n".join(parts)


def turn(run_id: str, message: str, *, timeout_s: int = 900) -> TurnResult:
    """Ответить на сообщение пользователя: текст и план. Реплики и стоимость — в чат и meta."""
    versions.recover(run_id)
    versions.ensure_v0(run_id)
    history = read_chat(run_id)[-HISTORY_MESSAGES:]
    n = len(read_chat(run_id)) + 1
    append_chat(run_id, "пользователь", message)
    result = call(
        PROMPT_DIALOG.read_text(encoding="utf-8"),
        build_document(run_id, message, history),
        DialogTurn,
        run_id=run_id,
        trace_name=f"dialog/turns/{n:04d}.raw.json",
        allowed_tools=ALLOWED_TOOLS_TEXT,
        timeout_s=timeout_s,
        model=DIALOG_MODEL,
    )
    _add_cost_to_meta(run_id, result.cost_usd)
    if not result.ok or result.value is None:
        reply = f"Не получилось ответить: {result.error or 'неизвестная ошибка'}. Попробуйте ещё раз."
        append_chat(run_id, "ассистент", reply, cost_usd=round(result.cost_usd, 4))
        return TurnResult(reply=reply, cost_usd=result.cost_usd)
    value = result.value
    append_chat(run_id, "ассистент", value.reply, plan=[op.model_dump(mode="json") for op in value.plan],
                cost_usd=round(result.cost_usd, 4))
    return TurnResult(reply=value.reply, plan=list(value.plan), cost_usd=result.cost_usd)
