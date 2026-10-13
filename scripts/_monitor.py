"""
Общее для инструментов мониторинга (2.7): разбор событий агента (сырой ответ и файл хода),
тексты строк ленты и виды ошибок инструментов.

События агента — те, что печатает `claude -p` (stream-json и список в .raw.json): assistant с
tool_use (вызов инструмента), user с tool_result (ответ инструмента, is_error — ошибка),
result (итог: стоимость, ходы, время).
"""

from __future__ import annotations

import json
from pathlib import Path

GARANT_PREFIX = "mcp__garant__"
# Служебный инструмент CLI: так модель сдаёт ответ по --json-schema. Не поиск — в счёт инструментов не идёт;
# его ошибка — «ответ не по схеме»: CLI вернул модели причину, и она исправила ответ сама
STRUCTURED_OUTPUT = "StructuredOutput"
SCHEMA_RETRY = "ответ не прошёл проверку CLI (модель исправила)"


def short_tool(name: str) -> str:
    """Имя инструмента без префикса сервера: mcp__garant__garant_search → garant_search."""
    return name.removeprefix(GARANT_PREFIX)


def error_kind(text: str) -> str:
    """Вид ошибки инструмента — по тексту ответа (как в разборе живой проверки 12.10)."""
    if "does not match required schema" in text or "could not be parsed as JSON" in text:
        return SCHEMA_RETRY
    if "No such tool available" in text:
        return "нет такого инструмента"
    if "denied because" in text or "Permission to use" in text:
        return "не разрешён агенту"
    if "transport dropped" in text or "session expired" in text or "ECONNRESET" in text:
        return "обрыв связи"
    if "timed out" in text or "timeout of" in text:
        return "таймаут"
    if "Нет прав" in text:
        return "нет прав"
    if "не найден" in text:
        return "не найдено"
    return "другая"


def _text_of(content) -> str:
    """Текст ответа инструмента: строка или список блоков {type: text, text}."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return json.dumps(content, ensure_ascii=False)


def tool_calls(events: list[dict]) -> list[dict]:
    """Вызовы инструментов агента по порядку: имя, вход, ошибка ли, вид ошибки, начало ответа."""
    calls: dict[str, dict] = {}
    order: list[str] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        for b in ((e.get("message") or {}).get("content") or []) if e.get("type") in ("assistant", "user") else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                calls[b["id"]] = {"name": short_tool(b.get("name", "")), "input": b.get("input"),
                                  "error": None, "answer": None}
                order.append(b["id"])
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                text = _text_of(b.get("content"))
                call = calls[b["tool_use_id"]]
                call["answer"] = text[:300]
                if b.get("is_error") or text.startswith("MCP server") or "Нет прав" in text[:100]:
                    # Ошибка служебного StructuredOutput — CLI вернул модели причину (не по схеме, не JSON)
                    call["error"] = SCHEMA_RETRY if call["name"] == STRUCTURED_OUTPUT else error_kind(text)
    return [calls[i] for i in order]


def load_raw(path: Path) -> list[dict]:
    """События из сырого ответа .raw.json; битый или не список — пусто."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def short_input(value, limit: int = 90) -> str:
    """Вход инструмента в одну строку: запрос поиска, topic, адрес."""
    if isinstance(value, dict):
        for key in ("query", "text", "url", "topic", "prompt"):
            if key in value:
                return f"{key}={str(value[key])[:limit]}"
        return json.dumps(value, ensure_ascii=False)[:limit]
    return str(value)[:limit]


def describe_agent_event(event: dict) -> list[str]:
    """Строки ленты для одного события агента: вызов инструмента, ошибка ответа, текст, итог."""
    out = []
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        mcp = [s.get("name") + ":" + s.get("status", "") for s in event.get("mcp_servers") or []]
        out.append("старт" + (f" · MCP {', '.join(mcp)}" if mcp else ""))
    elif kind == "assistant":
        for b in (event.get("message") or {}).get("content") or []:
            if b.get("type") == "tool_use":
                out.append(f"→ {short_tool(b.get('name', ''))} {short_input(b.get('input'))}")
            elif b.get("type") == "text" and b.get("text", "").strip():
                out.append("пишет: " + b["text"].strip().replace("\n", " ")[:120])
    elif kind == "user":
        for b in (event.get("message") or {}).get("content") or []:
            if b.get("type") == "tool_result":
                text = _text_of(b.get("content"))
                if b.get("is_error") or text.startswith("MCP server") or "Нет прав" in text[:100]:
                    out.append(f"⚠ ошибка инструмента ({error_kind(text)}): {text[:120]}")
    elif kind == "result":
        out.append(f"итог: {event.get('subtype')} · ${float(event.get('total_cost_usd') or 0):.2f}"
                   f" · ходов {event.get('num_turns')} · {round((event.get('duration_ms') or 0) / 1000)} с")
    return out


def describe_journal_event(e: dict) -> str | None:
    """Строка ленты для события журнала сессии; artifact и agent_* — не показываются (их видно иначе)."""
    ev = e["event"]
    if ev == "session_created":
        return f"сессия создана: {e.get('act_filename')} · тип {', '.join(e.get('act_types') or []) or '—'}"
    if ev == "node_started":
        return f"старт (вход: {', '.join(e.get('inputs') or [])})"
    if ev == "node_finished":
        return f"готов за {e.get('duration_s')} с · ${float(e.get('cost_usd') or 0):.2f} → {', '.join(e.get('outputs') or [])}"
    if ev == "node_failed":
        return f"⚠ СБОЙ: {e.get('error')}"
    if ev == "agent_attempt" and e.get("outcome") != "ok":
        return f"⚠ попытка {e.get('attempt')}/{e.get('max_attempts')}: {e.get('outcome')} за {e.get('duration_s')} с"
    if ev == "run_finished":
        return f"разбор закончен: {e.get('status')} · ${float(e.get('cost_usd') or 0):.2f}"
    if ev == "chat":
        return f"чат ({e.get('role')}): {e.get('text', '')[:120]}"
    if ev == "user_action":
        return f"действие: {e.get('action')}"
    if ev in ("version_created", "version_restored"):
        return f"{'версия' if ev == 'version_created' else 'возврат к версии'} {e.get('version')}"
    if ev == "session_closed":
        return f"сессия закрыта ({e.get('by')}) · всего ${float(e.get('cost_usd') or 0):.2f}"
    return None
