"""
Инструмент помощника «прочитать артефакт прогона» — локальный MCP-сервер stdio (итерация 4, 14.10, план помощника,
шаг 2).

Помощник после прогона отвечает по артефактам прогона (вердикты, таблицы проверки, гипотезы, карта, бланк, журнал
версий). Доступ к файлам машины ему не даём (изоляция агентов): этот сервер читает ТОЛЬКО файлы одного прогона —
папка задаётся переменной окружения P10_RUN_DIR, которую ставит конфиг MCP хода помощника.

Без внешних библиотек (пакета `mcp` в окружении нет): JSON-RPC 2.0 построчно по stdin/stdout — initialize,
notifications/initialized, ping, tools/list, tools/call (спецификация MCP 2025-11-25; проба 14.10 — CLI подключает
такой сервер). В stdout — только ответы протокола; всё остальное — в stderr.

Инструменты:
  list_artifacts()                          -> какие файлы прогона можно читать;
  read_artifact(path, query="", start_line=1) -> файл по строкам (кусками по READ_LINES) или только строки с query.

Запуск: python -m app.mcp_run (CLI запускает сам по конфигу; см. app/dialog.py).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Что помощнику можно читать: чистые артефакты прогона и снимки версий; сырые ответы моделей, логи, служебное — нет
_ALLOWED = re.compile(
    r"^(?:versions/v\d+/)?(?:01_document\.md|02_passport\.json|03_verdicts/\d{2}\.json|03_verdicts/\d{2}\.checks\.json"
    r"|04_buckets\.json|05_hypotheses/\d{2}\.json|06_map\.json|07_request\.json)$"
    r"|^(?:meta\.json|versions/state\.json|versions/journal\.jsonl)$"
)
READ_LINES = 300          # строк за один вызов read_artifact (ответ не раздувает контекст; дальше — start_line)
QUERY_LIMIT = 80          # строк-совпадений за один вызов с query
PROTOCOL_VERSION = "2025-11-25"

TOOLS = [
    {
        "name": "list_artifacts",
        "description": (
            "Список файлов результата разбора этого акта, которые можно прочитать через read_artifact: текст акта, "
            "паспорт, вердикты и таблицы проверки индикаторов, гипотезы, карта гипотез, бланк запроса, сведения "
            "о прогоне, состояние и журнал версий, снимки прошлых версий (versions/vN/…). Используй, когда нужно "
            "понять, какие файлы есть сейчас, — например, есть ли таблица проверки у индикатора или какие версии "
            "сохранены. Что лежит в каждом файле и какие в нём поля, описано в карте прогона — для этого список "
            "не нужен. Возвращает пути по одному на строку; содержимого файлов не возвращает. Параметров нет."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_artifact",
        "description": (
            "Прочитать файл результата разбора этого акта — единственный источник того, что сейчас стоит в Excel "
            "эксперта: вердикты, обоснования, таблицы проверки, строки карты, позиции запроса, версии. Используй "
            "всякий раз, когда отвечаешь про результат или готовишь правку: файлы меняются после каждой "
            "применённой правки, поэтому прочитанное раньше могло устареть. Не используй для актов и сведений "
            "вне этого разбора — для них Гарант и интернет. Без query возвращает файл кусками по 300 строк "
            "с номерами строк; если файл длиннее, в конце ответа подсказка, с какой строки (start_line) читать "
            "дальше. С query возвращает только строки, где встречается этот текст (без учёта регистра), с номерами, "
            "не больше 80 строк — так быстрее найти пункт акта («2.8.1»), строку карты («\"row_id\": 3») или позицию "
            "запроса («\"num\": \"12\"»). Читает только файлы из list_artifacts; сырые ответы агентов, логи "
            "и файлы вне этого прогона недоступны — на них вернётся ошибка с причиной."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Путь файла как в list_artifacts, например «03_verdicts/03.json» или «06_map.json»."},
                "query": {"type": "string",
                          "description": "Текст, который ищем в файле (без учёта регистра); пусто — читать файл подряд."},
                "start_line": {"type": "integer", "minimum": 1,
                               "description": "С какой строки читать файл подряд (по подсказке прошлого ответа); с query не используется."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]


def run_dir() -> Path:
    """Папка прогона из P10_RUN_DIR; без неё сервер ничего не читает."""
    value = os.environ.get("P10_RUN_DIR", "")
    if not value:
        raise RuntimeError("не задана папка прогона (P10_RUN_DIR)")
    return Path(value).resolve()


def list_artifacts(root: Path) -> str:
    """Вход: папка прогона. Выход: пути разрешённых файлов, по одному на строку."""
    paths = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    allowed = [p for p in paths if _ALLOWED.match(p)]
    return "\n".join(allowed) if allowed else "в папке прогона нет файлов для чтения"


def _resolve(root: Path, path: str) -> Path:
    """Путь внутри папки прогона и из разрешённых; иначе ValueError с понятной причиной."""
    rel = path.strip().lstrip("/")
    if not _ALLOWED.match(rel):
        raise ValueError(f"файл «{path}» читать нельзя: доступны только файлы из list_artifacts")
    full = (root / rel).resolve()
    if root not in full.parents:
        raise ValueError(f"файл «{path}» вне папки прогона")
    if not full.is_file():
        raise ValueError(f"файла «{path}» в прогоне нет")
    return full


def read_artifact(root: Path, path: str, query: str = "", start_line: int = 1) -> str:
    """Вход: папка прогона, путь, строка поиска, начальная строка. Выход: текст с номерами строк и подсказкой."""
    lines = _resolve(root, path).read_text(encoding="utf-8").splitlines()
    if query:
        needle = query.casefold()
        hits = [f"{i}: {line}" for i, line in enumerate(lines, start=1) if needle in line.casefold()]
        head = hits[:QUERY_LIMIT]
        tail = f"\n… ещё {len(hits) - QUERY_LIMIT} совпадений — уточни query" if len(hits) > QUERY_LIMIT else ""
        return ("\n".join(head) + tail) if hits else f"«{query}» в {path} не найдено"
    start = max(1, start_line)
    chunk = lines[start - 1:start - 1 + READ_LINES]
    body = "\n".join(f"{i}: {line}" for i, line in enumerate(chunk, start=start))
    end = start - 1 + len(chunk)
    more = f"\n… строки {start}–{end} из {len(lines)}; дальше — start_line={end + 1}" if end < len(lines) else ""
    return (body + more) if chunk else f"в {path} всего {len(lines)} строк"


def _call_tool(name: str, args: dict) -> tuple[str, bool]:
    """Вызов инструмента: (текст ответа, признак ошибки). Ошибка — текст причины, не исключение."""
    try:
        root = run_dir()
        if name == "list_artifacts":
            return list_artifacts(root), False
        if name == "read_artifact":
            return read_artifact(root, str(args.get("path", "")), str(args.get("query") or ""),
                                 int(args.get("start_line") or 1)), False
        return f"нет инструмента «{name}»", True
    except (ValueError, RuntimeError, OSError, UnicodeDecodeError) as exc:
        return str(exc), True


def handle(message: dict) -> dict | None:
    """Один запрос JSON-RPC → ответ (None — уведомление, отвечать не нужно)."""
    method, msg_id = message.get("method"), message.get("id")
    if msg_id is None:
        return None                                   # уведомления (notifications/initialized и др.)
    if method == "initialize":
        params = message.get("params") or {}
        result = {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "p10run", "version": "1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        text, is_error = _call_tool(params.get("name", ""), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"нет метода {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> None:
    """Цикл сервера: строка JSON на входе → строка JSON на выходе."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            print(f"mcp_run: не JSON: {line[:200]}", file=sys.stderr)
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
