"""Граница двух CLI: реальные формы событий, единые контракты и отказы без вызова моделей."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app import config, runner, trace
from app.contracts import ActPassportOutput, DialogTurn, GarantTurn, HypothesisSetOutput, MergeOutput, RequestOutput, VerdictOutput
from app.nodes.verdicts import INDICATOR_OUTPUT

FIXTURES = Path(__file__).parent / "fixtures"
THREAD = "01a18b62-1bba-7d33-a6eb-19798eed2862"
ANSWER = {"marker": "P10-957d8387403f", "total": 43, "phase": "first"}
USAGE = {"input_tokens": 5927, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
         "output_tokens": 31, "reasoning_output_tokens": 0}


class Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    marker: str
    total: int = Field(ge=0)
    phase: str


@pytest.fixture
def codex(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "codex", raising=False)
    # Любая попытка уйти по старому пути — ошибка теста, не живой вызов Claude.
    monkeypatch.setattr(runner, "_run_attempt", lambda *a, **kw: pytest.fail("Claude must not be called"))


def prepared(monkeypatch, replies):
    """Подмена только процесса: реальны сбор команды, разбор, повторы и pydantic."""
    calls = []
    replies = iter(replies)

    def execute(cmd, **kwargs):
        calls.append((cmd, kwargs))
        spec = next(replies)
        if isinstance(spec, Exception):
            raise spec
        answer = spec.get("answer", ANSWER)
        if answer is not None:
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
                answer if isinstance(answer, str) else json.dumps(answer))
        events = spec.get("events")
        if events is None:
            events = [
                {"type": "thread.started", "thread_id": spec.get("thread", THREAD)},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
                 "text": json.dumps(answer)}},
                {"type": "turn.completed", "usage": spec.get("usage", USAGE)},
            ]
        return SimpleNamespace(returncode=spec.get("code", 0),
                               stdout=spec.get("stdout", json.dumps(events)),
                               stderr=spec.get("stderr", ""))
    monkeypatch.setattr(runner, "_exec", execute)
    return calls


def invoke(**kwargs):
    return runner.call("проверь", "ТЕКСТ АКТА", Probe, run_id="probe",
                       trace_name="probe.raw.json", allowed_tools=[], **kwargs)


@pytest.mark.parametrize("backend,expected", [(None, "claude:sonnet"), ("claude", "claude:sonnet"),
                                            ("codex", "codex:gpt-6.1-terra")])
def test_backend_is_selected_at_startup(backend, expected):
    env = dict(os.environ)
    env.pop("P10_LLM_BACKEND", None)
    if backend is not None:
        env["P10_LLM_BACKEND"] = backend
    result = subprocess.run([sys.executable, "-c",
        "from app.config import LLM_BACKEND, MODEL; print(LLM_BACKEND+':'+MODEL)"],
        env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_invalid_backend_stops_startup():
    env = dict(os.environ, P10_LLM_BACKEND="unknown")
    result = subprocess.run([sys.executable, "-c", "import app.config"], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "P10_LLM_BACKEND" in result.stderr


def test_recorded_cli_reply_and_startup_notice_are_accepted(codex, tmp_runs_dir, monkeypatch):
    events = [json.loads(line) for line in (FIXTURES / "codex_first.events.jsonl").read_text().splitlines()]
    calls = prepared(monkeypatch, [{"events": events}])
    result = invoke()
    assert result.ok and result.value.model_dump() == ANSWER
    assert result.backend == "codex" and result.model == "gpt-6.1-terra"
    assert result.cost_known is False and result.token_usage == USAGE
    assert result.session_token_usage == USAGE and result.usage_complete
    cmd, args = calls[0]
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[cmd.index("--model") + 1] == "gpt-6.1-terra"
    assert "--ignore-user-config" in cmd and "--ignore-rules" in cmd
    assert "sandbox_mode=\"read-only\"" in cmd and "features.shell_tool=false" in cmd
    assert "ТЕКСТ АКТА" in args["document"] and "проверь" in args["document"]
    assert trace.run_dir("probe").joinpath("probe.raw.json").is_file()


def test_validation_retry_resumes_without_repeating_act(codex, tmp_runs_dir, monkeypatch):
    later = {**USAGE, "input_tokens": 6000, "output_tokens": 60}
    calls = prepared(monkeypatch, [{"answer": {**ANSWER, "total": -1}}, {"usage": later}])
    result = invoke()
    assert result.ok and result.attempts == 2
    assert calls[1][0][:3] == ["codex", "exec", "resume"]
    assert THREAD in calls[1][0]
    assert "ТЕКСТ АКТА" not in calls[1][1]["document"]
    assert "не прошёл проверку" in calls[1][1]["document"]
    assert result.token_usage == later  # накопленное по двум попыткам, без двойного сложения


def test_external_resume_counts_only_delta(codex, tmp_runs_dir, monkeypatch):
    events = [json.loads(line) for line in (FIXTURES / "codex_resume.events.jsonl").read_text().splitlines()]
    prepared(monkeypatch, [{"events": events, "answer": {**ANSWER, "total": 52, "phase": "resume"}}])
    result = invoke(resume_session_id=THREAD, resume_backend="codex", resume_token_base=USAGE)
    assert result.ok and result.token_usage["input_tokens"] == 6008
    assert result.token_usage["output_tokens"] == 31
    assert result.session_token_usage["input_tokens"] == 11935


@pytest.mark.parametrize("kwargs", [
    {"resume_session_id": THREAD},
    {"resume_session_id": THREAD, "resume_backend": "claude"},
    {"resume_session_id": "--last", "resume_backend": "codex", "resume_token_base": USAGE},
    {"max_budget_usd": 1.0},
    {"model": "gpt-6.1-sol"},
    {"allowed_tools": ["WebSearch"]},
    {"mcp_tools": ["not_a_garant_tool"]},
])
def test_unsupported_or_cross_backend_request_never_starts_process(codex, tmp_runs_dir, monkeypatch, kwargs):
    execute = prepared(monkeypatch, [])
    args = dict(instruction="проверь", document="акт", model_cls=Probe, run_id="probe",
                trace_name="probe.raw.json", allowed_tools=[])
    args.update(kwargs)
    with pytest.raises((ValueError, RuntimeError)):
        runner.call(**args)
    assert execute == []


@pytest.mark.parametrize("spec,kind", [
    ({"code": 7, "stderr": "service down"}, "process_error"),
    ({"code": 2, "stdout": "startup failed", "stderr": "invalid argument"}, "process_error"),
    ({"stdout": "not-json"}, "top_level_json_error"),
    ({"answer": None}, "no_result"),
    ({"answer": ""}, "no_result"),
    ({"answer": "broken-json"}, "result_json_error"),
    ({"answer": {**ANSWER, "total": -1}}, "validation_error"),
    ({"thread": "--last"}, "invalid_session"),
    ({"events": [{"type": "thread.started", "thread_id": THREAD},
                 {"type": "turn.failed", "error": {"message": "unavailable"}}]}, "api_error"),
    ({"events": [{"type": "thread.started", "thread_id": THREAD}]}, "no_result"),
    ({"events": [{"type": "thread.started", "thread_id": THREAD},
                 {"type": "item.completed", "item": {"type": "error", "message": "real error"}},
                 {"type": "turn.completed", "usage": USAGE}]}, "api_error"),
])
def test_failures_are_explicit_and_preserve_raw(codex, tmp_runs_dir, monkeypatch, spec, kind):
    prepared(monkeypatch, [spec])
    result = invoke(max_attempts=1)
    assert not result.ok and result.value is None
    assert kind in result.error
    assert (trace.run_dir("probe") / "probe.raw.json").is_file()


def test_timeout_preserves_partial_output_and_retries_from_start(codex, tmp_runs_dir, monkeypatch):
    timeout = subprocess.TimeoutExpired("codex", 1, output='[{"type":"turn.started"}]')
    calls = prepared(monkeypatch, [timeout, {}])
    result = invoke()
    assert result.ok and result.attempts == 2 and not result.usage_complete
    assert calls[1][0][:2] == ["codex", "exec"] and "resume" not in calls[1][0]
    assert len(list(trace.run_dir("probe").glob("probe.raw*.json"))) == 2


def test_wrong_resumed_thread_is_rejected(codex, tmp_runs_dir, monkeypatch):
    prepared(monkeypatch, [{"thread": "wrong"}])
    result = invoke(max_attempts=1, resume_session_id=THREAD, resume_backend="codex",
                    resume_token_base=USAGE)
    assert not result.ok and "session_mismatch" in result.error
    assert result.session_id is None and not result.usage_complete


def test_unexpected_tool_is_not_accepted_as_success(codex, tmp_runs_dir, monkeypatch):
    events = [{"type": "thread.started", "thread_id": THREAD},
              {"type": "item.completed", "item": {"type": "command_execution", "command": "echo nope"}},
              {"type": "turn.completed", "usage": USAGE}]
    prepared(monkeypatch, [{"events": events}])
    result = invoke(max_attempts=1)
    assert not result.ok and "unexpected_tool" in result.error


def test_api_keys_are_not_inherited(codex, tmp_runs_dir, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "TEST-OPENAI")
    monkeypatch.setenv("CODEX_API_KEY", "TEST-CODEX")
    calls = prepared(monkeypatch, [{}])
    assert invoke().ok
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY"} & calls[0][1]["env"].keys()


def test_usage_marks_run_cost_as_unknown(codex, tmp_runs_dir, monkeypatch):
    rid = trace.new_run("example.docx")
    prepared(monkeypatch, [{}])
    result = runner.call("проверь", "акт", Probe, run_id=rid, trace_name="probe.raw.json", allowed_tools=[])
    meta = json.loads((trace.run_dir(rid) / trace.META_FILE).read_text())
    assert result.ok and meta["cost_known"] is False
    assert meta["token_usage"]["input_tokens"] == 5927
    assert meta["llm_backend"] == "codex"


OUTPUTS = [ActPassportOutput, VerdictOutput, *INDICATOR_OUTPUT.values(), HypothesisSetOutput,
           MergeOutput, RequestOutput, DialogTurn, GarantTurn]


@pytest.mark.parametrize("model", OUTPUTS, ids=lambda m: m.__name__)
def test_output_schema_keeps_contract_rules(model):
    original = model.model_json_schema()
    schema = runner.codex_output_schema(original)
    assert original == model.model_json_schema(), "нельзя менять исходную схему"
    def check(before, after):
        if isinstance(before, dict):
            for key, value in before.items():
                if key not in ("default", "required", "additionalProperties"):
                    assert key in after
                    check(value, after[key])
            if "properties" in after:
                assert after["additionalProperties"] is False
                assert set(after["required"]) == set(after["properties"])
        elif isinstance(before, list):
            assert len(before) == len(after)
            for a, b in zip(before, after):
                check(a, b)
        else:
            assert before == after
    check(original, schema)


def test_call_text_uses_same_codex_contract_loop(codex, tmp_runs_dir, monkeypatch):
    prepared(monkeypatch, [{"answer": {"text": "готово"}}])
    result = runner.call_text("ответь коротко", "акт", run_id="probe",
                              trace_name="text.raw.json", allowed_tools=[])
    assert result.ok and result.value == "готово" and result.cost_known is False


def test_schema_preserves_property_literally_named_default():
    class NamedDefault(BaseModel):
        default: int = 3
    schema = runner.codex_output_schema(NamedDefault.model_json_schema())
    assert "default" in schema["properties"]
    assert schema["required"] == ["default"]
    assert "default" not in schema["properties"]["default"]  # удаляется аннотация, не поле


def test_open_dictionary_schema_is_not_silently_restricted():
    class Mapping(BaseModel):
        values: dict[str, str]
    with pytest.raises(ValueError, match="словар"):
        runner.codex_output_schema(Mapping.model_json_schema())


@pytest.mark.parametrize("bad", [{"input_tokens": -1, "output_tokens": 1},
                                {"input_tokens": True, "output_tokens": 1}])
def test_invalid_usage_is_explicit(codex, tmp_runs_dir, monkeypatch, bad):
    prepared(monkeypatch, [{"usage": bad}])
    result = invoke(max_attempts=1)
    assert not result.ok and not result.usage_complete and "usage_error" in result.error


def test_missing_usage_is_unknown_not_zero(codex, tmp_runs_dir, monkeypatch):
    prepared(monkeypatch, [{"usage": {}}])
    result = invoke()
    assert result.ok and not result.cost_known
    assert result.token_usage is None and not result.usage_complete


def test_resumed_usage_cannot_decrease(codex, tmp_runs_dir, monkeypatch):
    prepared(monkeypatch, [{"usage": {**USAGE, "input_tokens": 1}}])
    result = invoke(max_attempts=1, resume_session_id=THREAD, resume_backend="codex",
                    resume_token_base=USAGE)
    assert not result.ok and "usage_error" in result.error and not result.usage_complete


def test_missing_binary_is_not_retried(codex, tmp_runs_dir, monkeypatch):
    calls = prepared(monkeypatch, [FileNotFoundError()])
    with pytest.raises(RuntimeError, match="codex"):
        invoke()
    assert len(calls) == 1


def test_legacy_meta_is_readable_with_original_provenance(valid_run_meta):
    from app.contracts import RunMeta
    meta = RunMeta.model_validate(valid_run_meta)
    assert meta.llm_backend == "claude" and meta.llm_model == "sonnet"
    assert meta.cost_known and meta.token_usage == {}


def test_recovered_connection_accepts_completed_answer_and_records_notice(codex, tmp_runs_dir, monkeypatch):
    events = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "turn.started"},
        {"type": "error", "message": "Reconnecting... 2/5 (workspace routing discovery timed out)"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(ANSWER)}},
        {"type": "turn.completed", "usage": USAGE},
    ]
    calls = prepared(monkeypatch, [{"events": events}])
    result = invoke(max_attempts=1)
    assert result.ok and result.attempts == 1 and len(calls) == 1
    assert any(e.get("event") == "cli_notice" and "Reconnecting" in e.get("message", "")
               for e in trace.read_events("probe"))


def test_reconnect_without_completed_turn_is_still_error(codex, tmp_runs_dir, monkeypatch):
    events = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "error", "message": "Reconnecting... 5/5 (workspace routing discovery failed)"},
        {"type": "turn.failed", "error": {"message": "Connection failed"}},
    ]
    prepared(monkeypatch, [{"events": events}])
    result = invoke(max_attempts=1)
    assert not result.ok and "api_error" in result.error


def test_metadata_fallback_notice_is_visible_and_completed_answer_is_accepted(codex, tmp_runs_dir, monkeypatch):
    message = "Model metadata for `gpt-6.1-terra` not found. Defaulting to fallback metadata; this can degrade performance and cause issues."
    events = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "item.completed", "item": {"type": "error", "message": message}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(ANSWER)}},
        {"type": "turn.completed", "usage": USAGE},
    ]
    prepared(monkeypatch, [{"events": events}])
    result = invoke(max_attempts=1)
    assert result.ok and result.value.model_dump() == ANSWER
    assert any(e.get("event") == "cli_notice" and e.get("message") == message
               for e in trace.read_events("probe"))


def test_unrelated_error_remains_failure_even_after_completed_turn(codex, tmp_runs_dir, monkeypatch):
    events = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "error", "message": "Unexpected authorization error"},
        {"type": "turn.completed", "usage": USAGE},
    ]
    prepared(monkeypatch, [{"events": events}])
    result = invoke(max_attempts=1)
    assert not result.ok and "api_error" in result.error


def test_transport_fallback_accepts_completed_answer_and_records_notice(codex, tmp_runs_dir, monkeypatch):
    message = "Falling back from WebSockets to HTTPS transport. workspace routing discovery timed out"
    events = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "item.completed", "item": {"type": "error", "message": message}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(ANSWER)}},
        {"type": "turn.completed", "usage": USAGE},
    ]
    calls = prepared(monkeypatch, [{"events": events}])
    result = invoke(max_attempts=1)
    assert result.ok and result.value.model_dump() == ANSWER and len(calls) == 1
    assert any(e.get("event") == "cli_notice" and e.get("message") == message
               for e in trace.read_events("probe"))


def test_transport_fallback_without_completed_turn_remains_error(codex, tmp_runs_dir, monkeypatch):
    events = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "item.completed", "item": {"type": "error", "message": "Falling back from WebSockets to HTTPS transport. timed out"}},
        {"type": "turn.failed", "error": {"message": "Connection failed"}},
    ]
    prepared(monkeypatch, [{"events": events}])
    result = invoke(max_attempts=1)
    assert not result.ok and "api_error" in result.error
