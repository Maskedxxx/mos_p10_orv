"""Локальный MCP-инструмент чтения интернет-версии Гаранта через отдельный браузер.

Работает только с числовым ID документа и блока. Подключение к агентам — отдельный этап.
"""
from __future__ import annotations

import json
import subprocess
import sys

TOOL = {
    "name": "garant_web",
    "description": (
        "Прочитать документ в авторизованной интернет-версии Гаранта. "
        "block читает блок по topic и entry; page читает одну часть документа; "
        "search ищет фразу без учёта регистра и различий пробелов по пяти частям за вызов. "
        "Для продолжения поиска передавай все поля next; для длинного текста — next_offset и expected_hash=content_hash. "
        "Поиск завершён для всего документа только при последовательном чтении с первой части без пропусков, "
        "когда больше нет next или failed. Идентификаторы блоков из "
        "candidates — возможные места совпадения; проверяй текст блока и его контекст. "
        "Таблицы передаются как строки и ячейки; изображения не читаются и в поиске не участвуют — смотри warnings. "
        "Источник и редакцию проверяй отдельно; ошибка не доказывает отсутствие документа."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["block", "page", "search"]},
            "topic": {"type": "string", "pattern": "^[0-9]{1,12}$", "description": "ID документа Гаранта."},
            "entry": {"type": "string", "pattern": "^[0-9]{1,12}$", "description": "ID блока для block."},
            "page": {"type": "integer", "minimum": 1, "description": "Номер части для page."},
            "offset": {"type": "integer", "minimum": 0, "description": "Смещение по next_offset для block/page."},
            "expected_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$", "description": "content_hash предыдущего текста либо expected_hash из next поиска."},
            "query": {"type": "string", "minLength": 1, "maxLength": 250, "description": "Точная фраза для search."},
            "start_page": {"type": "integer", "minimum": 1, "description": "Начальная часть поиска из next."},
            "match_offset": {"type": "integer", "minimum": 0, "description": "Смещение совпадений из next."},
        },
        "required": ["action", "topic"],
        "additionalProperties": False,
    },
}


def call_browser(arguments: dict) -> dict:
    """Данные поступают в контейнер по stdin, а не в аргументах процесса или журнале."""
    if not isinstance(arguments, dict):
        return {"ok": False, "error": {"code": "invalid_input", "message": "Нужен объект параметров."}}
    try:
        result = subprocess.run(
            ["docker", "exec", "-i", "p10-garant-browser", "timeout", "--kill-after=5s", "210s",
             "node", "/app/p10/garant_web.cjs"],
            input=json.dumps(arguments, ensure_ascii=False) + "\n", text=True, capture_output=True, timeout=240,
            check=False,
        )
        if result.returncode == 124:
            return {"ok": False, "error": {"code": "timeout", "message": "Чтение не завершено за отведённое время."}}
        if result.returncode:
            return {"ok": False, "error": {"code": "browser_unavailable", "message": "Браузер Гаранта недоступен."}}
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise ValueError("invalid response")
        return response
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "timeout", "message": "Чтение не завершено за отведённое время."}}
    except (OSError, ValueError):
        return {"ok": False, "error": {"code": "browser_unavailable", "message": "Не удалось получить ответ браузера."}}


def handle(message: dict) -> dict | None:
    """Один JSON-RPC запрос; уведомления не требуют ответа."""
    method, msg_id = message.get("method"), message.get("id")
    if msg_id is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": (message.get("params") or {}).get("protocolVersion", "2025-11-25"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "p10-garant-web", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") == TOOL["name"]:
            response = call_browser(params.get("arguments", {}))
        else:
            response = {"ok": False, "error": {"code": "unknown_tool", "message": "Инструмент не найден."}}
        result = {"content": [{"type": "text", "text": json.dumps(response, ensure_ascii=False)}],
                  "isError": not response["ok"]}
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Метод недоступен."}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC object required")
            reply = handle(message)
        except (ValueError, TypeError, AttributeError):
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Некорректный JSON-RPC."}}
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
