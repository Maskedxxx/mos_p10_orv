"""Indicator 3 MCP bridge: scoped tools, exact cache and compact redaction replies.

stdio is used by both CLIs; upstream remains the official Garant MCP endpoint.
No token or upstream error body is written to output, cache or diagnostics.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

ENDPOINT = "https://mcp.garant.ru/mcp"
BASE_URL = "https://internet.garant.ru"
ALLOWED = frozenset({"garant_find_hyperlinks", "garant_search", "garant_document_redactions", "garant_snippets"})
PROTOCOL = "2025-06-18"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("upstream redirect refused")


class Upstream:
    def __init__(self):
        self.session = None
        self.ready = False
        self.counter = 0
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def send(self, method, params=None, notification=False):
        token = os.environ.get("GARANT_TOKEN", "")
        if not token or "\n" in token or "\r" in token:
            raise RuntimeError("Garant token unavailable")
        self.counter += 1
        message = {"jsonrpc": "2.0", "method": method}
        if not notification:
            message["id"] = self.counter
        if params is not None:
            message["params"] = params
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = Request(ENDPOINT, json.dumps(message).encode(), headers=headers, method="POST")
        with self.opener.open(request, timeout=90) as response:
            self.session = response.headers.get("Mcp-Session-Id", self.session)
            if notification:
                return {}
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                # Read only until our response event; do not wait for an open SSE stream to close.
                data = []
                for raw in response:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        reply = json.loads("\n".join(data))
                        data = []
                        if reply.get("id") == message["id"]:
                            break
                else:
                    raise RuntimeError("MCP response missing")
            else:
                reply = json.load(response)
        if reply.get("id") != message["id"] or "error" in reply or "result" not in reply:
            raise RuntimeError("MCP response error")
        return reply["result"]

    def request(self, method, params=None):
        if not self.ready:
            result = self.send("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                               "clientInfo": {"name": "p10-indicator3", "version": "1"}})
            if result.get("protocolVersion") != PROTOCOL:
                raise RuntimeError("Unsupported upstream MCP protocol")
            self.send("notifications/initialized", notification=True)
            self.ready = True
        try:
            return self.send(method, params)
        except Exception:
            self.ready = False
            self.session = None
            raise


def error_result():
    return {"isError": True, "content": [{"type": "text", "text": "Запрос Гаранта не выполнен; ответ не кэширован. Можно повторить или отметить непроверенность."}]}


def compact_redactions(result):
    """Удалить только rs_Old; остальные записи и поля сохранить без изменений."""
    reply = deepcopy(result)
    for block in reply.get("content", []):
        if block.get("type") != "text":
            continue
        try:
            rows = json.loads(block.get("text", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
            block["text"] = json.dumps(
                [row for row in rows if row.get("status") != "rs_Old"], ensure_ascii=False,
            )
    return reply


def save_reply(path, result):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(result, stream, ensure_ascii=False)
    temporary.replace(path)


class Bridge:
    def __init__(self, upstream, cache_dir: Path, audit_dir: Path | None = None):
        self.upstream = upstream
        self.cache_dir = cache_dir
        self.audit_dir = audit_dir
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def for_model(self, name, arguments, key, result):
        if name != "garant_document_redactions":
            return result
        if self.audit_dir is not None:
            path = self.audit_dir / f"{self.cache_dir.parent.name}-{key}.json"
            try:
                save_reply(path, {"arguments": arguments, "result": result})
            except OSError:
                # Без аудиторской копии полный ответ остаётся доступен в трассе тула.
                return result
        return compact_redactions(result)

    def tools(self):
        tools, cursor = [], None
        while True:
            result = self.upstream.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(deepcopy(t) for t in result.get("tools", []) if t.get("name") in ALLOWED)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        for tool in tools:
            if tool["name"] == "garant_find_hyperlinks":
                schema = tool["inputSchema"]
                schema.get("properties", {}).pop("base_url", None)
                if "required" in schema:
                    schema["required"] = [k for k in schema["required"] if k != "base_url"]
        return {"tools": tools}

    def call(self, name, arguments):
        if name not in ALLOWED or not isinstance(arguments, dict):
            return error_result()
        args = deepcopy(arguments)
        # Key is exact input (dictionary order is immaterial), not a fuzzy query match.
        key = hashlib.sha256(json.dumps([name, args], ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
        path = self.cache_dir / (key + ".json")
        try:
            if path.is_file():
                return self.for_model(name, arguments, key, json.loads(path.read_text()))
        except (OSError, ValueError):
            pass
        if name == "garant_find_hyperlinks":
            args["base_url"] = BASE_URL
        try:
            result = self.upstream.request("tools/call", {"name": name, "arguments": args})
            if not isinstance(result, dict) or result.get("isError") or not isinstance(result.get("content"), list):
                return error_result()
            # Defensive: neither a token echo nor a textual API error becomes a successful cache entry.
            token = os.environ.get("GARANT_TOKEN")
            if token and token in json.dumps(result, ensure_ascii=False):
                return error_result()
            for block in result["content"]:
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if isinstance(text, str) and text.lstrip().lower().startswith(("<!doctype html", "<html")):
                        return error_result()
                    try:
                        payload = json.loads(text)
                    except ValueError:
                        continue
                    if isinstance(payload, dict) and payload.get("error"):
                        return error_result()
        except Exception:
            return error_result()
        try:
            save_reply(path, result)
        except OSError:
            pass  # A local cache failure must not lose a successful upstream reply.
        return self.for_model(name, arguments, key, result)

    def handle(self, message):
        msg_id, method = message.get("id"), message.get("method")
        if msg_id is None:
            return None
        try:
            if method == "initialize":
                result = {"protocolVersion": message.get("params", {}).get("protocolVersion", PROTOCOL),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "p10-garant", "version": "1"}}
            elif method == "ping":
                result = {}
            elif method in ("resources/list", "resources/templates/list"):
                result = {"resources" if method == "resources/list" else "resourceTemplates": []}
            elif method == "tools/list":
                result = self.tools()
            elif method == "tools/call":
                params = message.get("params") or {}
                result = self.call(params.get("name"), params.get("arguments", {}))
            else:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Unsupported method"}}
        except Exception:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": "Garant unavailable"}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main():
    cache_dir = os.environ.get("P10_GARANT_CACHE_DIR")
    if not cache_dir:
        raise SystemExit("Missing invocation cache directory")
    audit_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    bridge = Bridge(Upstream(), Path(cache_dir), audit_dir)
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = bridge.handle(message)
        except (ValueError, TypeError, AttributeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
