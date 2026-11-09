import json
import subprocess
from types import SimpleNamespace

from app import mcp_garant_web


def test_mcp_lists_only_web_tool_without_calling_browser(monkeypatch):
    monkeypatch.setattr(mcp_garant_web, 'call_browser', lambda _: (_ for _ in ()).throw(AssertionError()))
    reply = mcp_garant_web.handle({'id': 1, 'method': 'tools/list'})
    assert [tool['name'] for tool in reply['result']['tools']] == ['garant_web']
    assert mcp_garant_web.handle({'method': 'notifications/initialized'}) is None


def test_response_and_failures_are_not_reported_as_success(monkeypatch):
    def simulate(*args, **kwargs):
        assert 'query' not in args[0]
        assert json.loads(kwargs['input'])['query'] == 'срок'
        return SimpleNamespace(returncode=0, stdout=json.dumps({'ok': False, 'error': {'code': 'auth_required'}}))
    monkeypatch.setattr(mcp_garant_web.subprocess, 'run', simulate)
    reply = mcp_garant_web.handle({'id': 2, 'method': 'tools/call',
        'params': {'name': 'garant_web', 'arguments': {'action': 'search', 'topic': '1', 'query': 'срок'}}})
    assert reply['result']['isError'] is True
    assert json.loads(reply['result']['content'][0]['text'])['error']['code'] == 'auth_required'


def test_timeout_and_bad_output_do_not_leak_container_output(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('private', 1, output='secret')
    monkeypatch.setattr(mcp_garant_web.subprocess, 'run', timeout)
    result = mcp_garant_web.call_browser({'topic': '1'})
    assert result['error']['code'] == 'timeout' and 'secret' not in json.dumps(result)
    monkeypatch.setattr(mcp_garant_web.subprocess, 'run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout='private', stderr='secret'))
    result = mcp_garant_web.call_browser({'topic': '1'})
    assert result['error']['code'] == 'browser_unavailable' and 'secret' not in json.dumps(result)
    monkeypatch.setattr(mcp_garant_web.subprocess, 'run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=124, stdout='', stderr=''))
    assert mcp_garant_web.call_browser({'topic': '1'})['error']['code'] == 'timeout'


def test_web_reader_uses_shared_browser_container(monkeypatch):
    def simulate(args, **kwargs):
        assert args[:4] == ["docker", "exec", "-i", "p10-garant-browser"]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True}))
    monkeypatch.setattr(mcp_garant_web.subprocess, "run", simulate)
    assert mcp_garant_web.call_browser({"action": "page", "topic": "1", "page": 1})["ok"]
