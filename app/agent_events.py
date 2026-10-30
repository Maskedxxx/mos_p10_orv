"""Чтение инструментальных событий Codex для обоих чатов и отчётов, без выполнения вызовов."""
import json


def codex_tool_event(event: dict) -> dict | None:
    if event.get("type") not in ("item.started", "item.updated", "item.completed"):
        return None
    item = event.get("item") or {}
    kind = item.get("type")
    if kind == "mcp_tool_call":
        name = f"mcp__{item.get('server', '')}__{item.get('tool', '')}"
        args = item.get("arguments") or {}
        answer = item.get("result")
    elif kind == "web_search":
        action = item.get("action") or {}
        opened = action.get("type") in ("open_page", "find_in_page")
        name = "WebFetch" if opened else "WebSearch"
        args = {"url": action.get("url", ""), "query": item.get("query") or action.get("query") or "",
                "pattern": action.get("pattern", "")}
        answer = None
    else:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"text": args}
    return {"id": item.get("id"), "name": name, "input": args,
            "is_error": item.get("status") == "failed" or bool(item.get("error")),
            "answer": item.get("error") or answer, "status": item.get("status")}
