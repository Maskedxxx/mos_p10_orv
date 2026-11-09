"""Связь индикаторов с двумя MCP-серверами без вызовов моделей и внешнего API."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from app import config, runner
from app.garant_proxy import indicator_mcp_config
from app.contracts import Verdict

WEB = 'mcp__garantweb__garant_web'
THREAD = '01a18b62-1bba-7d33-a6eb-19798eed2862'


@pytest.mark.parametrize('number', [3, 4, 10, 11])
def test_both_clis_receive_web_reader_and_original_garant(number, tmp_runs_dir, monkeypatch):
    monkeypatch.setattr(runner, '_mcp_env', lambda *a, **k: {'GARANT_TOKEN': 'secret-test-token'})
    builtin, methods = config.indicator_tools(number)
    assert WEB in methods and 'garant_search' in methods
    with indicator_mcp_config(number, 'run') as path:
        content = Path(path).read_text()
        servers = json.loads(content)['mcpServers']
        assert 'secret-test-token' not in content
        assert servers['garantweb'] == {
            'type': 'stdio', 'command': sys.executable,
            'args': [str(config.PROJECT_ROOT / 'app/mcp_garant_web.py')], 'env': {'GARANT_TOKEN': ''},
        }
        command = runner._build_cmd('task', '{}', builtin, None, None, mcp_tools=methods, mcp_config=path)
        allowed = command[command.index('--allowedTools') + 1].split()
        assert WEB in allowed and 'mcp__garant__garant_search' in allowed
        assert command[command.index('--mcp-config') + 1] == path
        invocation = runner._CodexInvocation('run', f'03_verdicts/{number:02d}.raw.json',
            'gpt-6.1-terra', {'type': 'object', 'properties': {}}, None, builtin, methods, path)
        assert ('garantweb', 'garant_web') in invocation.allowed_mcp
        assert ('garant', 'garant_search') in invocation.allowed_mcp
        assert invocation.mcp_options['mcp_servers.garantweb.env.GARANT_TOKEN'] == ''
        assert invocation.mcp_options['mcp_servers.garantweb.enabled_tools'] == ['garant_web']
        assert invocation.mcp_options['mcp_servers.garantweb.tool_timeout_sec'] == 250
        assert 'secret-test-token' not in json.dumps(invocation.mcp_options)
        assert ('command' in servers['garant']) == (number == 3)


def test_web_reader_is_absent_from_other_indicators():
    assert {n for n in range(1, 13) if WEB in config.indicator_tools(n)[1]} == {3, 4, 10, 11}


def test_codex_refuses_modified_web_server_and_unapproved_methods(tmp_runs_dir, monkeypatch):
    monkeypatch.setattr(runner, '_mcp_env', lambda *a, **k: {'GARANT_TOKEN': 'secret-test-token'})
    builtin, methods = config.indicator_tools(4)
    with indicator_mcp_config(4, 'run') as path:
        data = json.loads(Path(path).read_text())
        data['mcpServers']['garantweb']['args'] = ['/tmp/untrusted.py']
        Path(path).write_text(json.dumps(data))
        with pytest.raises(ValueError, match='веб-чтения'):
            runner._CodexInvocation('run', '03_verdicts/04.raw.json', 'gpt-6.1-terra',
                {'type': 'object', 'properties': {}}, None, builtin, methods, path)
        with pytest.raises(ValueError, match='неизвестный инструмент'):
            runner._CodexInvocation('run', '03_verdicts/04.raw.json', 'gpt-6.1-terra',
                {'type': 'object', 'properties': {}}, None, builtin, ['mcp__garantweb__other'], path)


def test_codex_accepts_only_permitted_web_call_event(tmp_runs_dir, monkeypatch, valid_verdict_ok):
    monkeypatch.setattr(config, 'LLM_BACKEND', 'codex')
    monkeypatch.setattr(runner, '_mcp_env', lambda *a, **k: {'GARANT_TOKEN': 'secret-test-token'})
    def fake_exec(cmd, **kwargs):
        Path(cmd[cmd.index('--output-last-message') + 1]).write_text(json.dumps(valid_verdict_ok))
        events = [
            {'type': 'thread.started', 'thread_id': THREAD},
            {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'server': 'garantweb',
                'tool': 'garant_web', 'arguments': {'action': 'block', 'topic': '409114710', 'entry': '1035'}}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 10}},
        ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(events), stderr='')
    monkeypatch.setattr(runner, '_exec', fake_exec)
    builtin, methods = config.indicator_tools(11)
    with indicator_mcp_config(11, 'run') as path:
        result = runner.call('задача', 'акт', Verdict,
            run_id='run', trace_name='03_verdicts/11.raw.json', allowed_tools=builtin,
            mcp_tools=methods, mcp_config=path, max_attempts=1)
    assert result.ok, result.error
