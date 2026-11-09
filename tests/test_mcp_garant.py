import io
import json
from pathlib import Path

import pytest

from app.mcp_garant import ALLOWED, BASE_URL, Bridge, Upstream, compact_redactions
from app.garant_proxy import indicator_mcp_config


class FakeUpstream:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def request(self, method, params):
        self.calls.append((method, params))
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return {"content": [{"type": "text", "text": '{"documents": [], "totalDocs": 0}'}], "isError": False}


def test_redactions_removes_only_old_and_preserves_every_other_field():
    statuses = ["rs_Old", "rs_Actual", "rs_ActualAbolished", "rs_ActualPreactive",
                "rs_New", "rs_NewPreactive", "rs_NewAbolished", "unknown", None]
    rows = [{"status": status, "topic": i, "activity": [{"from": "01.01.2026"}],
             "changingDocuments": [{"entry": i}], "extra": {"keep": True}}
            for i, status in enumerate(statuses)]
    reply = {"content": [{"type": "text", "text": json.dumps(rows)},
                         {"type": "text", "text": "not JSON"}], "isError": False}
    before = json.dumps(reply, sort_keys=True)
    result = compact_redactions(reply)
    assert json.loads(result["content"][0]["text"]) == rows[1:]
    assert result["content"][1] == reply["content"][1]
    assert result["isError"] is False
    assert json.dumps(reply, sort_keys=True) == before


@pytest.mark.parametrize("payload", [{"message": "unknown format"}, ["unknown entry"], []])
def test_redactions_unrecognised_payload_is_not_guessed(payload):
    reply = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    assert json.loads(compact_redactions(reply)["content"][0]["text"]) == payload


def test_redactions_cache_and_persistent_audit_keep_full_reply(tmp_path):
    rows = [{"status": "rs_Old", "topic": 1}, {"status": "rs_Actual", "topic": 2}]
    full = {"content": [{"type": "text", "text": json.dumps(rows)}]}
    upstream = FakeUpstream([full])
    cache, audit = tmp_path / "invocation" / "cache", tmp_path / "audit"
    bridge = Bridge(upstream, cache, audit)
    first = bridge.call("garant_document_redactions", {"topic": 2})
    assert json.loads(first["content"][0]["text"]) == [rows[1]]
    assert Bridge(upstream, cache, audit).call("garant_document_redactions", {"topic": 2}) == first
    assert len(upstream.calls) == 1
    cached = json.loads(next(cache.glob("*.json")).read_text())
    stored = json.loads(next(audit.glob("*.json")).read_text())
    assert cached == full and stored == {"arguments": {"topic": 2}, "result": full}


def test_exact_success_reused_including_empty_search_and_cli_restart(tmp_path):
    upstream = FakeUpstream()
    bridge = Bridge(upstream, tmp_path)
    first = bridge.call("garant_search", {"query": "текст", "page": 1})
    assert not first["isError"]
    assert bridge.call("garant_search", {"page": 1, "query": "текст"}) == first
    assert Bridge(upstream, tmp_path).call("garant_search", {"query": "текст", "page": 1}) == first
    assert len(upstream.calls) == 1
    bridge.call("garant_search", {"query": "текст", "page": 2})
    bridge.call("garant_search", {"query": "другой текст", "page": 1})
    assert len(upstream.calls) == 3


@pytest.mark.parametrize("failure", [RuntimeError("private exception"), {"isError": True, "content": []},
                                     {"content": [{"type": "text", "text": '{"error": "unavailable"}'}]}])
def test_failures_are_not_cached_and_do_not_expose_upstream_error(tmp_path, failure):
    upstream = FakeUpstream([failure])
    bridge = Bridge(upstream, tmp_path)
    first = bridge.call("garant_search", {"query": "текст"})
    assert first["isError"] and "private" not in json.dumps(first)
    assert not list(tmp_path.glob("*.json"))
    assert not bridge.call("garant_search", {"query": "текст"})["isError"]
    assert len(upstream.calls) == 2


def test_html_error_is_not_cached_as_success(tmp_path):
    html = {"content": [{"type": "text", "text": "<!DOCTYPE html><html>Техническая страница</html>"}]}
    upstream = FakeUpstream([html, {"content": [{"type": "text", "text": '{"documents": []}'}]}])
    bridge = Bridge(upstream, tmp_path)
    assert bridge.call("garant_search", {"query": "запрос"})["isError"]
    assert not list(tmp_path.glob("*.json"))
    assert not bridge.call("garant_search", {"query": "запрос"}).get("isError", False)
    assert len(upstream.calls) == 2


def test_base_url_is_fixed_and_exports_are_not_forwarded(tmp_path):
    upstream = FakeUpstream()
    bridge = Bridge(upstream, tmp_path)
    args = {"text": "реквизиты", "base_url": "https://internet.garant.ru/#/document/"}
    bridge.call("garant_find_hyperlinks", args)
    assert upstream.calls[0][1]["arguments"]["base_url"] == BASE_URL
    assert args["base_url"].endswith("/#/document/")  # caller input not mutated
    assert bridge.call("garant_export_html", {"topic": "1"})["isError"]
    assert len(upstream.calls) == 1


def test_token_echo_never_reaches_output_or_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("GARANT_TOKEN", "secret-test-token")
    bridge = Bridge(FakeUpstream([{"content": [{"type": "text", "text": "secret-test-token"}]}]), tmp_path)
    result = bridge.call("garant_search", {})
    assert result["isError"] and "secret-test-token" not in json.dumps(result)
    assert not list(tmp_path.glob("*.json"))


def test_tools_list_filters_permissions_and_hides_base_url(tmp_path):
    names = list(ALLOWED) + ["garant_export_html"]
    upstream = FakeUpstream([{"tools": [{"name": n, "inputSchema": {"type": "object", "properties": {"base_url": {"type": "string"}}, "required": ["base_url"]}} for n in names]}])
    result = Bridge(upstream, tmp_path).tools()
    assert {t["name"] for t in result["tools"]} == ALLOWED
    item = next(t for t in result["tools"] if t["name"] == "garant_find_hyperlinks")
    assert "base_url" not in item["inputSchema"]["properties"]
    assert item["inputSchema"]["required"] == []


def test_cache_is_unique_per_invocation_and_removed_afterwards(tmp_runs_dir, monkeypatch):
    monkeypatch.setenv("GARANT_TOKEN", "secret-test-token")
    with indicator_mcp_config(3, "run") as first:
        config = json.loads(Path(first).read_text())
        cache = Path(config["mcpServers"]["garant"]["env"]["P10_GARANT_CACHE_DIR"])
        with indicator_mcp_config(3, "run") as second:
            assert first != second
        assert "secret-test-token" not in Path(first).read_text()
        assert config["mcpServers"]["garantweb"]["env"] == {"GARANT_TOKEN": ""}
    assert not Path(first).exists() and not cache.exists()
    for n in (4, 10, 11):
        with indicator_mcp_config(n, "run") as other:
            servers = json.loads(Path(other).read_text())["mcpServers"]
            assert set(servers) == {"garant", "garantweb"}
            assert servers["garant"]["url"] == "https://mcp.garant.ru/mcp"
            assert servers["garantweb"]["env"] == {"GARANT_TOKEN": ""}
            assert "secret-test-token" not in Path(other).read_text()
        assert not Path(other).exists()
    with indicator_mcp_config(1, "run") as other:
        assert other is None


@pytest.mark.parametrize("sse", [False, True])
def test_upstream_protocol_json_and_sse_without_network(monkeypatch, sse):
    monkeypatch.setenv("GARANT_TOKEN", "secret-test-token")
    class Response(io.BytesIO):
        def __init__(self, result, msg_id):
            data = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}).encode()
            super().__init__(b"event: message\ndata: " + data + b"\n\n" if sse else data)
            self.headers = {"Content-Type": "text/event-stream" if sse else "application/json", "Mcp-Session-Id": "session"}
    class Opener:
        def __init__(self): self.calls = []
        def open(self, request, timeout):
            message = json.loads(request.data)
            self.calls.append((message, request.headers))
            result = {"protocolVersion": "2025-06-18"} if message["method"] == "initialize" else {"tools": []}
            return Response(result, message.get("id"))
    upstream = Upstream()
    opener = Opener()
    upstream.opener = opener
    assert upstream.request("tools/list", {}) == {"tools": []}
    assert [c[0]["method"] for c in opener.calls] == ["initialize", "notifications/initialized", "tools/list"]
    assert opener.calls[-1][1]["Mcp-session-id"] == "session"


def test_claude_and_codex_use_same_scoped_proxy_without_token_in_config(tmp_runs_dir, monkeypatch):
    from app import runner
    monkeypatch.setattr(runner, "_mcp_env", lambda: {"GARANT_TOKEN": "secret-test-token"})
    with indicator_mcp_config(3, "run") as path:
        command = runner._build_cmd("task", "{}", [], None, None,
                                    mcp_tools=sorted(ALLOWED), mcp_config=path)
        assert command[command.index("--mcp-config")+1] == path
        invocation = runner._CodexInvocation("run", "03_verdicts/03.raw.json", "gpt-6.1-terra",
                                              {"type": "object", "properties": {}}, None, [], sorted(ALLOWED), path)
        assert invocation.allowed_mcp == {("garant", method) for method in ALLOWED}
        assert invocation.mcp_options["mcp_servers.garant.env_vars"] == ["GARANT_TOKEN"]
        assert "secret-test-token" not in json.dumps(invocation.mcp_options)
        assert "secret-test-token" not in Path(path).read_text()
        with pytest.raises(ValueError, match="обёртка"):
            runner._CodexInvocation("run", "03.raw.json", "gpt-6.1-terra", {}, None, [], ["garant_export_html"], path)
        original = Path(path).read_text()
        wrong_audit = json.loads(original)
        wrong_audit["mcpServers"]["garant"]["args"][1] = "/tmp/another-run-audit"
        Path(path).write_text(json.dumps(wrong_audit))
        with pytest.raises(ValueError, match="обёртка"):
            runner._CodexInvocation("run", "03.raw.json", "gpt-6.1-terra", {}, None, [], ["garant_search"], path)
        Path(path).write_text(original)
        malicious = json.loads(Path(path).read_text())
        malicious["mcpServers"]["garant"]["env"]["P10_GARANT_CACHE_DIR"] = "/tmp/another-run"
        Path(path).write_text(json.dumps(malicious))
        with pytest.raises(ValueError, match="обёртка"):
            runner._CodexInvocation("run", "03.raw.json", "gpt-6.1-terra", {}, None, [], ["garant_search"], path)
