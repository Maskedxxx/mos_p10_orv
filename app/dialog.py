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
import os
import time
from dataclasses import dataclass, field

from app import versions
from app.config import ALLOWED_TOOLS_TEXT, DIALOG_MODEL, PROMPT_DIALOG
from app.contracts import DialogTurn, Operation, Remark
from app.nodes.hypotheses import _add_cost_to_meta
from app.runner import call
from app.trace import run_dir, save_bytes

CHAT = "dialog/chat.jsonl"
REMARKS = "dialog/remarks.json"   # очередь замечаний из файла эксперта
STATUS = "dialog/status.json"     # что идёт по прогону прямо сейчас; нет файла — ничего
# Процесс сервиса, записавший состояние: состояние от прежнего процесса — действие прервано перезапуском
SERVER_ID = f"{os.getpid()}-{int(time.time())}"
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
    extra = {k: v for k, v in extra.items() if v is not None}   # пустые поля (нет сессии, нет цены) не пишутся
    entry = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "role": role, "text": text, **extra}
    path = run_dir(run_id) / CHAT
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _json_block(title: str, raw: str) -> str:
    return f"## {title}\n\n```json\n{raw.strip()}\n```\n"


def shared_context(run_id: str) -> str:
    """
    Акт и паспорт — за жизнь прогона не меняются (2.6): уходят в системный промпт, и каждый
    следующий ход читает их из кэша, а не пишет заново. Порядок блоков для модели прежний.
    """
    root = run_dir(run_id)
    parts = [f"## ПОЛНЫЙ ТЕКСТ АКТА\n\n{(root / '01_document.md').read_text(encoding='utf-8')}\n"]
    if (root / "02_passport.json").is_file():
        parts.append(_json_block("ПАСПОРТ АКТА (JSON)", (root / "02_passport.json").read_text(encoding="utf-8")))
    return "\n".join(parts)


def build_document(run_id: str, message: str, history: list[dict]) -> str:
    """Вход модели в stdin: текущая версия прогона, история чата, сообщение пользователя — последним блоком.
    Акт и паспорт — не здесь, а в shared_context (системный промпт)."""
    root = run_dir(run_id)
    verdicts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((root / "03_verdicts").glob("[0-9][0-9].json"))]
    for v in verdicts:
        v.pop("attempts", None)
    state = versions.load_state(run_id)
    parts = [
        _json_block("ВЕРДИКТЫ (JSON)", json.dumps(verdicts, ensure_ascii=False, indent=2)),
        _json_block("КАРТА ГИПОТЕЗ (JSON)", (root / "06_map.json").read_text(encoding="utf-8")),
        _json_block("ЗАПРОС ПОКАЗАТЕЛЕЙ (JSON)", (root / "07_request.json").read_text(encoding="utf-8")),
        _json_block("СОСТОЯНИЕ ВЕРСИЙ (JSON)", state.model_dump_json(indent=2) if state else "{}"),
    ]
    lines = [f"{m['role']}: {m['text']}" for m in history] or ["(начало диалога)"]
    parts.append("## ИСТОРИЯ ДИАЛОГА\n\n" + "\n\n".join(lines) + "\n")
    parts.append(f"## СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ\n\n{message}\n")
    return "\n".join(parts)


def turn(run_id: str, message: str, *, session: str | None = None, timeout_s: int = 900) -> TurnResult:
    """Ответить на сообщение пользователя: текст и план. Реплики и стоимость — в чат и meta; session — чья реплика."""
    versions.recover(run_id)
    versions.ensure_v0(run_id)
    history = read_chat(run_id)[-HISTORY_MESSAGES:]
    n = len(read_chat(run_id)) + 1
    append_chat(run_id, "пользователь", message, session=session)
    result = call(
        PROMPT_DIALOG.read_text(encoding="utf-8"),
        build_document(run_id, message, history),
        DialogTurn,
        run_id=run_id,
        trace_name=f"dialog/turns/{n:04d}.raw.json",
        allowed_tools=ALLOWED_TOOLS_TEXT,
        timeout_s=timeout_s,
        model=DIALOG_MODEL,
        shared_context=shared_context(run_id),
        short_cache_ttl=False,   # срок кэша 1 час: человек думает между сообщениями дольше 5 минут
    )
    _add_cost_to_meta(run_id, result.cost_usd)
    if not result.ok or result.value is None:
        reply = f"Не получилось ответить: {result.error or 'неизвестная ошибка'}. Попробуйте ещё раз."
        append_chat(run_id, "ассистент", reply, cost_usd=round(result.cost_usd, 4), mark="сбой")
        return TurnResult(reply=reply, cost_usd=result.cost_usd)
    value = result.value
    append_chat(run_id, "ассистент", value.reply, plan=[op.model_dump(mode="json") for op in value.plan],
                cost_usd=round(result.cost_usd, 4))
    return TurnResult(reply=value.reply, plan=list(value.plan), cost_usd=result.cost_usd)


def save_remarks(run_id: str, file_name: str, remarks: list[Remark]) -> None:
    """Очередь замечаний из файла эксперта; новый файл заменяет прежнюю очередь."""
    data = {"file": file_name, "next": 0, "remarks": [r.model_dump(mode="json") for r in remarks]}
    save_bytes(run_id, REMARKS, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def _load_remarks(run_id: str) -> dict | None:
    path = run_dir(run_id) / REMARKS
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def remarks_progress(run_id: str) -> tuple[int, int] | None:
    """(сколько замечаний уже ушло в диалог, сколько всего); None — файла эксперта не было."""
    data = _load_remarks(run_id)
    return None if data is None else (data["next"], len(data["remarks"]))


def next_remark(run_id: str) -> Remark | None:
    """Взять следующее замечание и сдвинуть очередь; None — очередь разобрана."""
    data = _load_remarks(run_id)
    if data is None or data["next"] >= len(data["remarks"]):
        return None
    remark = Remark.model_validate(data["remarks"][data["next"]])
    data["next"] += 1
    save_bytes(run_id, REMARKS, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    return remark


def set_status(run_id: str, state: str, title: str, detail: str) -> None:
    """Что идёт по прогону: «думает» (ход помощника), «шаг» (шаг плана, возврат версии), «решение» (ждёт человека)."""
    data = {"state": state, "title": title, "detail": detail,
            "since": datetime.datetime.now().isoformat(timespec="seconds"), "server": SERVER_ID}
    save_bytes(run_id, STATUS, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def read_status(run_id: str) -> dict | None:
    path = run_dir(run_id) / STATUS
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):   # файл пишется атомарно, но страница не должна падать из-за него
        return None


def clear_status(run_id: str) -> None:
    (run_dir(run_id) / STATUS).unlink(missing_ok=True)
