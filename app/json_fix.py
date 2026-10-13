"""
Починка битого JSON ответа модели по известной схеме контракта (2.7, 13.10.2026, разбор паспорта).

Модель отдаёт ответ через инструмент StructuredOutput CLI. Большой ответ (паспорт акта) она
иногда ломает: пропускает закрывающую скобку раздела, и следующий раздел оказывается внутри
предыдущего. CLI отвергает такой ответ, модель переписывает его — и урезает содержимое.
Поэтому runner берёт всегда ПЕРВЫЙ ответ модели и, если он не разбирается, чинит его здесь.

Чинится то, что можно поставить на место однозначно, зная схему:
1. пропущенные закрывающие скобки:
   - в середине — ключ встретился в объекте, где его по схеме нет, но он есть у объекта выше:
     закрываем всё до этого объекта, скобки ставим перед запятой, отделяющей ключ;
   - в конце — закрываем всё, что осталось открытым;
2. неэкранированные кавычки внутри строки: кавычка, после которой не идёт продолжение JSON
   (`:`, `}`, `]` или запятая перед следующим ключом или значением), считается частью текста.

Результат — текст JSON. Правильность структуры и значений проверяет контракт (pydantic)
у вызывающего; если починка не помогла, проверка это покажет.
"""

from __future__ import annotations

from typing import Any

# Символы, с которых начинается значение или ключ после запятой
_VALUE_START = set('"{[-0123456789tfn}]')


def _resolve(node: dict[str, Any] | None, defs: dict[str, Any], container: str) -> dict[str, Any] | None:
    """Схема для открываемого объекта ('{') или массива ('['): $ref и варианты anyOf (Optional) раскрываются."""
    if not isinstance(node, dict):
        return None
    if "$ref" in node:
        return _resolve(defs.get(node["$ref"].rsplit("/", 1)[-1]), defs, container)
    if "anyOf" in node:
        for option in node["anyOf"]:
            found = _resolve(option, defs, container)
            if found is not None:
                return found
        return None
    wanted = "object" if container == "{" else "array"
    if node.get("type") == wanted or (wanted == "object" and "properties" in node):
        return node
    return None


def _next_significant(text: str, i: int) -> tuple[str, int]:
    """Следующий непробельный символ и его позиция ('' в конце текста)."""
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return (text[i], i) if i < len(text) else ("", i)


def _is_string_end(text: str, i: int) -> bool:
    """Кавычка на позиции i закрывает строку, если за ней идёт продолжение JSON."""
    ch, j = _next_significant(text, i + 1)
    if ch in ("", ":", "}", "]"):
        return True
    if ch == ",":
        after, _ = _next_significant(text, j + 1)
        return after in _VALUE_START
    return False


def repair_json(text: str, schema: dict[str, Any]) -> str:
    """Вернуть текст с поставленными на место скобками и экранированными кавычками (см. docstring модуля)."""
    defs = schema.get("$defs", {})
    out: list[str] = []
    # Кадр открытого контейнера: скобка, схема, последний ключ (для объекта), позиция последней запятой в out
    stack: list[dict[str, Any]] = []
    last_comma: int | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            # Строка целиком: внутренние кавычки экранируются
            j = i + 1
            buf = ['"']
            while j < len(text):
                c = text[j]
                if c == "\\" and j + 1 < len(text):
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                if c == '"':
                    if _is_string_end(text, j):
                        break
                    buf.append('\\"')
                    j += 1
                    continue
                buf.append(c)
                j += 1
            buf.append('"')
            string_text = "".join(buf)
            nxt, _ = _next_significant(text, j + 1)
            if nxt == ":":
                key = string_text[1:-1]
                # Ключ не на своём месте: закрыть контейнеры до объекта, которому он принадлежит
                owner = next((k for k in range(len(stack) - 1, -1, -1)
                              if key in ((stack[k]["schema"] or {}).get("properties") or {})), None)
                before = next((t for t in reversed(out) if t.strip()), "")
                if owner is not None and owner != len(stack) - 1 and last_comma is not None and before == ",":
                    closers = "".join("}" if f["bracket"] == "{" else "]" for f in reversed(stack[owner + 1:]))
                    out.insert(last_comma, closers)
                    del stack[owner + 1:]
                if stack and stack[-1]["bracket"] == "{":
                    stack[-1]["key"] = key
            out.append(string_text)
            i = j + 1
            continue
        if ch in "{[":
            parent = stack[-1] if stack else None
            if parent is None:
                node = schema
            elif parent["bracket"] == "{":
                node = ((parent["schema"] or {}).get("properties") or {}).get(parent["key"])
            else:
                node = (parent["schema"] or {}).get("items")
            stack.append({"bracket": ch, "schema": _resolve(node, defs, ch) if parent else schema, "key": None})
        elif ch in "}]":
            if stack:
                stack.pop()
        elif ch == ",":
            last_comma = len(out)
        out.append(ch)
        i += 1
    # Конец текста: закрыть всё, что осталось открытым
    out.extend("}" if f["bracket"] == "{" else "]" for f in reversed(stack))
    return "".join(out)
