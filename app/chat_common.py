"""
Общее у двух чатов сервиса — помощника по прогону (app/dialog.py) и Гарант-помощника (app/garant_chat.py).
27.10 (модульность): раньше каждый чат держал свою копию этих констант и функций.

    константы хода   пометка «Гарант недоступен», задание первого хода, признак потерянной сессии CLI,
                     инструменты Гаранта, роли реплик
    файл реплик      чтение и дописывание chat.jsonl (у помощника — dialog/chat.jsonl прогона,
                     у Гарант-помощника — chat.jsonl беседы); запись в журнал сессии — у каждого чата своя
    шаги агента      вызовы инструментов хода из turns/NNNN*.stream.jsonl (их пишет runner по ходу работы)
                     и строка «что делаю» для человека

Логика хода — продолжить сессию CLI или начать новую — у чатов своя и намеренно разная, она в их модулях.
Сколько последних реплик уходит модели в новой сессии — config.HISTORY_MESSAGES.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import get_args

from app.config import GARANT_ALL_TOOLS
from app.contracts import ChatEntry, ChatRole

# Пометка служебной строки хода, когда файла токена Гаранта нет (15.10)
GARANT_OFF_NOTE = ("Гарант сейчас недоступен (нет файла токена): инструментов Гаранта в этом ходе нет. "
                   "Если пользователь просит найти или проверить в Гаранте — скажи ему об этом.")
# Задание первого хода новой сессии; роль и правила — в системном промпте чата (промпт 07 или 08)
NEW_SESSION_INSTRUCTION = "Ответь на сообщение пользователя — оно последним блоком входа."
# Что CLI пишет, когда сессии для --resume у него нет (проба 15.10: код 1, «No conversation found with session ID»)
SESSION_GONE = "No conversation found"
# Гарант в чате (решение владельца 14.10): все инструменты всегда, и лимитированные. Набор за сессию не меняется —
# смена инструментов посреди сессии сбрасывает кэш. Выгрузки (30 в месяц на всех) агент делает сам и предупреждает
# в ответе; сколько потрачено — в журнал (помощник по прогону) или строкой в беседу (Гарант-помощник)
GARANT_TOOLS = list(GARANT_ALL_TOOLS)
ROLES = get_args(ChatRole)   # пользователь, ассистент, сервис — контракт реплики ChatEntry
# Кто в журнале сессии (events.jsonl) для каждой роли чата
CHAT_WHO = {"пользователь": "пользователь", "ассистент": "помощник", "сервис": "система"}


# ---- Файл реплик ----

def read_chat_file(path: Path) -> list[ChatEntry]:
    """Реплики чата по порядку, проверенные контрактом ChatEntry; нет файла — пусто."""
    if not path.is_file():
        return []
    return [ChatEntry.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def append_chat_file(path: Path, role: str, text: str, **extra) -> ChatEntry:
    """Дописать реплику в файл чата: время, роль, текст и поля; пустые поля (нет сессии, нет цены) не пишутся."""
    assert role in ROLES, role
    entry = ChatEntry(at=datetime.datetime.now().isoformat(timespec="seconds"), role=role, text=text,
                      **{k: v for k, v in extra.items() if v is not None})
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry.model_dump(mode="json", exclude_none=True), ensure_ascii=False) + "\n")
    return entry


# ---- Шаги агента ----

def turn_tools(turns_dir: Path, n: int) -> tuple[list[dict], set[str]]:
    """
    Вызовы инструментов хода n по порядку (без дублей событий) и id вызовов, на которые инструмент ответил
    ошибкой, — из файлов хода turns_dir/NNNN*.stream.jsonl (их пишет runner по ходу работы).
    """
    uses: list[dict] = []
    seen: set[str] = set()
    errors: set[str] = set()
    for path in sorted(turns_dir.glob(f"{n:04d}*.stream.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line).get("event") or {}
            except (ValueError, AttributeError):
                continue
            if not isinstance(event, dict) or event.get("type") not in ("assistant", "user"):
                continue
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result" and block.get("is_error"):
                    errors.add(str(block.get("tool_use_id")))
                elif block.get("type") == "tool_use" and block.get("id") not in seen:
                    seen.add(block.get("id"))
                    uses.append(block)
    return uses, errors


# Шаги агента для пользователя (шаг 7): вызов инструмента → строка «что делаю»
_STEP_TITLES = {
    "mcp__p10run__list_artifacts": "смотрю список файлов результата",
    "mcp__garant__garant_document_redactions": "смотрю редакции акта в Гаранте",
    "mcp__garant__garant_snippets": "ищу место в документе Гаранта",
    "mcp__garant__garant_sutyazhnik": "ищу судебную практику в Гаранте («Сутяжник»)",
    "mcp__garant__garant_limits": "смотрю остаток лимитов Гаранта",
    "mcp__garant__garant_export_block_html": "выгружаю текст пункта из Гаранта (платно)",
    "mcp__garant__garant_export_html": "выгружаю текст документа из Гаранта (платно)",
    "mcp__garant__garant_export_rtf": "выгружаю документ из Гаранта в RTF (платно)",
    "mcp__garant__garant_export_odt": "выгружаю документ из Гаранта в ODT (платно)",
    "mcp__garant__garant_export_pdf": "выгружаю документ из Гаранта в PDF (платно)",
    "mcp__garant__garant_export_image": "выгружаю картинку из Гаранта (платно)",
    "mcp__garant__garant_export_formula": "выгружаю формулу из Гаранта (платно)",
    "mcp__garant__garant_document_info": "смотрю карточку документа в Гаранте",
    "mcp__garant__garant_find_hyperlinks": "распознаю ссылки на акты в тексте (Гарант)",
    "mcp__garant__garant_find_modified": "проверяю в Гаранте, менялись ли документы",
    "mcp__garant__garant_block_changed": "проверяю в Гаранте, менялись ли пункты",
    "mcp__garant__garant_prime_categories": "смотрю разделы новостей Гаранта",
    "mcp__garant__garant_prime_news": "ищу новости в Гаранте",
}


def tool_step_text(name: str, args: dict) -> str | None:
    """Строка шага по вызову инструмента; None — служебный вызов (ответ по схеме), не показывается."""
    if name == "StructuredOutput":
        return None
    if name == "mcp__p10run__read_artifact":
        query = args.get("query")
        return f"читаю {args.get('path', '')}" + (f" — ищу «{query}»" if query else "")
    if name == "mcp__garant__garant_search":
        return f"ищу в Гаранте: {args.get('query') or args.get('text') or ''}".rstrip(": ")
    if name == "WebSearch":
        return f"ищу в интернете: {args.get('query', '')}"
    if name == "WebFetch":
        return f"открываю {args.get('url', '')}"
    return _STEP_TITLES.get(name, f"вызываю {name.removeprefix('mcp__')}")
