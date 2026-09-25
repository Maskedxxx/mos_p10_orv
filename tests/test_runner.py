"""
Тесты app/runner.py (шаг 2).

Нижний уровень (`subprocess.run`) подменяется моками — реально прогоняется
вся логика ретраев, разбора вывода и валидации. Живой тест (маркер `live`)
дёргает настоящий `claude -p` и по умолчанию не запускается.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import BaseModel, ConfigDict

from app.contracts import Verdict
from app.runner import CallResult, _build_cmd, call, call_text


# ---- Вспомогательные фабрики для мока subprocess.run ----


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    """Подделка под subprocess.CompletedProcess — код обращается только к этим полям."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _result_event(
    result: str,
    session_id: str = "sess-1",
    cost: float = 0.01,
    is_error: bool = False,
    num_turns: int = 1,
    permission_denials: list | None = None,
) -> dict[str, Any]:
    return {
        "type": "result",
        "result": result,
        "session_id": session_id,
        "total_cost_usd": cost,
        "is_error": is_error,
        "num_turns": num_turns,
        "permission_denials": permission_denials or [],
    }


def _stdout_with_events(*events: dict[str, Any]) -> str:
    return json.dumps(list(events))


def _extract_message(cmd: list[str]) -> str:
    """Достать текст сообщения-инструкции из argv: элемент прямо перед --json-schema."""
    idx = cmd.index("--json-schema")
    return cmd[idx - 1]


def _extract_message_text(cmd: list[str]) -> str:
    """Достать текст сообщения-инструкции из argv команды call_text (без --json-schema):
    элемент прямо перед --output-format."""
    idx = cmd.index("--output-format")
    return cmd[idx - 1]


def _extract_allowed_tools(cmd: list[str]) -> str:
    idx = cmd.index("--allowedTools")
    return cmd[idx + 1]


# ---- Тесты ----


def test_success_first_attempt(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """1. Успех с первой попытки: ok=True, attempts=1, стоимость из события."""

    stdout = _stdout_with_events(
        _result_event(json.dumps(valid_verdict_ok), session_id="sess-abc", cost=0.1234)
    )
    mock_run = Mock(return_value=_proc(stdout))
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call(
        "инструкция", "текст акта", Verdict,
        run_id="run1", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
    )

    assert result.ok is True
    assert result.attempts == 1
    assert result.cost_usd == pytest.approx(0.1234)
    assert result.session_id == "sess-abc"
    assert result.value == Verdict.model_validate(valid_verdict_ok)
    assert result.error is None
    assert mock_run.call_count == 1


def test_broken_json_result_then_valid(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """2. Битый JSON в result, потом валидный: attempts=2, вторая команда с --resume
    тем же session_id, документ второй раз в stdin не передаётся."""

    bad_stdout = _stdout_with_events(_result_event("{не json", session_id="sess-xyz"))
    good_stdout = _stdout_with_events(
        _result_event(json.dumps(valid_verdict_ok), session_id="sess-xyz", cost=0.02)
    )
    mock_run = Mock(side_effect=[_proc(bad_stdout), _proc(good_stdout)])
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call(
        "инструкция", "текст акта", Verdict,
        run_id="run2", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
    )

    assert result.ok is True
    assert result.attempts == 2

    first_cmd = mock_run.call_args_list[0].args[0]
    second_cmd = mock_run.call_args_list[1].args[0]
    assert "--resume" not in first_cmd
    assert "--resume" in second_cmd
    idx = second_cmd.index("--resume")
    assert second_cmd[idx + 1] == "sess-xyz"

    # Документ второй раз не передаётся: input отсутствует, stdin = DEVNULL
    second_kwargs = mock_run.call_args_list[1].kwargs
    assert "input" not in second_kwargs
    assert second_kwargs["stdin"] == subprocess.DEVNULL
    # На первой попытке документ передавался через input
    assert mock_run.call_args_list[0].kwargs["input"] == "текст акта"


def test_validation_error_then_valid(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """3. Ответ не прошёл pydantic-валидацию, потом прошёл: attempts=2, текст ошибки
    pydantic попал в промпт ретрая."""

    invalid_payload = {"indicator": 1, "name": "х"}  # не хватает обязательных полей
    bad_stdout = _stdout_with_events(
        _result_event(json.dumps(invalid_payload), session_id="sess-val")
    )
    good_stdout = _stdout_with_events(
        _result_event(json.dumps(valid_verdict_ok), session_id="sess-val", cost=0.03)
    )
    mock_run = Mock(side_effect=[_proc(bad_stdout), _proc(good_stdout)])
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call(
        "инструкция", "текст акта", Verdict,
        run_id="run3", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
    )

    assert result.ok is True
    assert result.attempts == 2

    second_cmd = mock_run.call_args_list[1].args[0]
    assert "--resume" in second_cmd
    retry_message = _extract_message(second_cmd)
    # Текст ошибки pydantic (упоминание отсутствующего поля) должен попасть в промпт
    assert "verdict" in retry_message or "Field required" in retry_message
    assert "исправленный JSON" in retry_message


def test_all_attempts_fail(tmp_runs_dir, monkeypatch):
    """4. Все попытки провалились: ok=False, attempts == max_attempts, error заполнен,
    исключения не бросается."""

    bad_stdout = _stdout_with_events(_result_event("{не json"))
    mock_run = Mock(return_value=_proc(bad_stdout))
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call(
        "инструкция", "текст акта", Verdict,
        run_id="run4", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
        max_attempts=3,
    )

    assert result.ok is False
    assert result.attempts == 3
    assert result.value is None
    assert result.error is not None
    assert mock_run.call_count == 3


def test_timeout_retries_without_resume(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """5. Таймаут: ретрай идёт БЕЗ --resume."""

    good_stdout = _stdout_with_events(
        _result_event(json.dumps(valid_verdict_ok), session_id="sess-t", cost=0.01)
    )
    timeout_exc = subprocess.TimeoutExpired(cmd=["claude"], timeout=900, output="частичный вывод")
    mock_run = Mock(side_effect=[timeout_exc, _proc(good_stdout)])
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call(
        "инструкция", "текст акта", Verdict,
        run_id="run5", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
    )

    assert result.ok is True
    assert result.attempts == 2
    second_cmd = mock_run.call_args_list[1].args[0]
    assert "--resume" not in second_cmd
    # Документ должен быть отправлен заново (сессии не было)
    assert mock_run.call_args_list[1].kwargs.get("input") == "текст акта"


def test_is_error_true_retries_without_resume(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """6. is_error: true распознан, ретрай без resume, текст ошибки API сохранён."""

    error_stdout = _stdout_with_events(
        _result_event("API Error: 400 bad request", session_id="sess-e", is_error=True)
    )
    good_stdout = _stdout_with_events(
        _result_event(json.dumps(valid_verdict_ok), session_id="sess-e2", cost=0.01)
    )
    mock_run = Mock(side_effect=[_proc(error_stdout), _proc(good_stdout)])
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call(
        "инструкция", "текст акта", Verdict,
        run_id="run6", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
    )

    assert result.ok is True
    assert result.attempts == 2
    second_cmd = mock_run.call_args_list[1].args[0]
    assert "--resume" not in second_cmd


def test_raw_saved_every_attempt(tmp_runs_dir, monkeypatch):
    """7. Сырой ответ сохранён на КАЖДОЙ попытке, включая упавшие — три файла
    в папке прогона при трёх попытках."""

    stdouts = [
        _stdout_with_events(_result_event("{плохой1")),
        _stdout_with_events(_result_event("{плохой2")),
        _stdout_with_events(_result_event("{плохой3")),
    ]
    mock_run = Mock(side_effect=[_proc(s) for s in stdouts])
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call(
        "инструкция", "текст акта", Verdict,
        run_id="run7", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
        max_attempts=3,
    )

    assert result.attempts == 3
    saved = sorted((tmp_runs_dir / "run7" / "03_verdicts").glob("01.raw*.json"))
    assert len(saved) == 3
    # Порядок имён файлов (01.raw.json, .attempt2, .attempt3) лексикографически не
    # совпадает с порядком попыток — сравниваем содержимое как множество
    contents = {p.read_text(encoding="utf-8") for p in saved}
    assert contents == set(stdouts)


def test_cost_summed_across_attempts(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """8. Стоимость суммируется по всем попыткам."""

    bad_stdout = _stdout_with_events(_result_event("{плохой", session_id="s1", cost=0.02))
    good_stdout = _stdout_with_events(
        _result_event(json.dumps(valid_verdict_ok), session_id="s1", cost=0.03)
    )
    mock_run = Mock(side_effect=[_proc(bad_stdout), _proc(good_stdout)])
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call(
        "инструкция", "текст акта", Verdict,
        run_id="run8", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
    )

    assert result.cost_usd == pytest.approx(0.05)


def test_allowed_tools_empty_and_nonempty(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """9. allowed_tools попадает в команду: пустой список -> "", непустой -> через пробел."""

    stdout = _stdout_with_events(
        _result_event(json.dumps(valid_verdict_ok), session_id="s1")
    )

    mock_run = Mock(return_value=_proc(stdout))
    monkeypatch.setattr(subprocess, "run", mock_run)
    call(
        "инструкция", "текст акта", Verdict,
        run_id="run9a", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
    )
    cmd = mock_run.call_args_list[0].args[0]
    assert _extract_allowed_tools(cmd) == ""

    mock_run2 = Mock(return_value=_proc(stdout))
    monkeypatch.setattr(subprocess, "run", mock_run2)
    call(
        "инструкция", "текст акта", Verdict,
        run_id="run9b", trace_name="03_verdicts/01.raw.json",
        allowed_tools=["WebSearch", "WebFetch"],
    )
    cmd2 = mock_run2.call_args_list[0].args[0]
    assert _extract_allowed_tools(cmd2) == "WebSearch WebFetch"


def test_document_none_no_stdin(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """10. document=None — stdin не используется (не передаётся input)."""

    stdout = _stdout_with_events(
        _result_event(json.dumps(valid_verdict_ok), session_id="s1")
    )
    mock_run = Mock(return_value=_proc(stdout))
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call(
        "инструкция", None, Verdict,
        run_id="run10", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
    )

    assert result.ok is True
    kwargs = mock_run.call_args_list[0].kwargs
    assert "input" not in kwargs
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_max_budget_usd_flag_present_when_set(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """12. max_budget_usd передаётся в CLI как --max-budget-usd, когда задан."""

    stdout = _stdout_with_events(
        _result_event(json.dumps(valid_verdict_ok), session_id="s1")
    )
    mock_run = Mock(return_value=_proc(stdout))
    monkeypatch.setattr(subprocess, "run", mock_run)

    call(
        "инструкция", "текст акта", Verdict,
        run_id="run_budget_set", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
        max_budget_usd=0.5,
    )

    cmd = mock_run.call_args_list[0].args[0]
    assert "--max-budget-usd" in cmd
    idx = cmd.index("--max-budget-usd")
    assert cmd[idx + 1] == "0.5"


def test_max_budget_usd_flag_absent_when_none(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """12. При max_budget_usd=None (по умолчанию) флаг --max-budget-usd в команде отсутствует."""

    stdout = _stdout_with_events(
        _result_event(json.dumps(valid_verdict_ok), session_id="s1")
    )
    mock_run = Mock(return_value=_proc(stdout))
    monkeypatch.setattr(subprocess, "run", mock_run)

    call(
        "инструкция", "текст акта", Verdict,
        run_id="run_budget_none", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
    )

    cmd = mock_run.call_args_list[0].args[0]
    assert "--max-budget-usd" not in cmd


def test_permission_denials_accumulated_in_result(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """13. permission_denials из событий накапливаются по всем попыткам в CallResult."""

    bad_stdout = _stdout_with_events(
        _result_event("{плохой", session_id="s1", permission_denials=["WebSearch"])
    )
    good_stdout = _stdout_with_events(
        _result_event(
            json.dumps(valid_verdict_ok), session_id="s1", permission_denials=["WebFetch"]
        )
    )
    mock_run = Mock(side_effect=[_proc(bad_stdout), _proc(good_stdout)])
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call(
        "инструкция", "текст акта", Verdict,
        run_id="run_denials", trace_name="03_verdicts/01.raw.json", allowed_tools=[],
    )

    assert result.ok is True
    assert result.attempts == 2
    assert result.permission_denials == ["WebSearch", "WebFetch"]


# ---- Тесты call_text (шаг 3) ----


def test_call_text_success_returns_text(tmp_runs_dir, monkeypatch):
    """14. call_text возвращает текст ответа при успехе."""

    stdout = _stdout_with_events(
        _result_event("## Саммари\n\nТекст.", session_id="sess-text1", cost=0.02)
    )
    mock_run = Mock(return_value=_proc(stdout))
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call_text(
        "инструкция", "текст акта",
        run_id="run_text1", trace_name="02_summary.raw.json", allowed_tools=[],
    )

    assert result.ok is True
    assert result.value == "## Саммари\n\nТекст."
    assert result.attempts == 1
    assert result.session_id == "sess-text1"
    assert result.cost_usd == pytest.approx(0.02)


def test_call_text_empty_response_retried(tmp_runs_dir, monkeypatch):
    """15. Пустой/пробельный ответ ретраится полным повтором (документ отправлен заново)."""

    blank_stdout = _stdout_with_events(_result_event("   ", session_id="sess-blank"))
    good_stdout = _stdout_with_events(_result_event("текст ответа", session_id="sess-blank2"))
    mock_run = Mock(side_effect=[_proc(blank_stdout), _proc(good_stdout)])
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call_text(
        "инструкция", "текст акта",
        run_id="run_text2", trace_name="02_summary.raw.json", allowed_tools=[],
    )

    assert result.ok is True
    assert result.attempts == 2
    assert result.value == "текст ответа"
    assert mock_run.call_args_list[1].kwargs.get("input") == "текст акта"


def test_call_text_retries_without_resume(tmp_runs_dir, monkeypatch):
    """16. Ретрай call_text идёт БЕЗ --resume — ни в одной из команд флага нет."""

    bad_stdout = _stdout_with_events(_result_event("", session_id="sess-r1"))
    good_stdout = _stdout_with_events(_result_event("текст", session_id="sess-r2"))
    mock_run = Mock(side_effect=[_proc(bad_stdout), _proc(good_stdout)])
    monkeypatch.setattr(subprocess, "run", mock_run)

    call_text(
        "инструкция", "текст акта",
        run_id="run_text3", trace_name="02_summary.raw.json", allowed_tools=[],
    )

    first_cmd = mock_run.call_args_list[0].args[0]
    second_cmd = mock_run.call_args_list[1].args[0]
    assert "--resume" not in first_cmd
    assert "--resume" not in second_cmd


def test_call_text_no_json_schema_in_command(tmp_runs_dir, monkeypatch):
    """17. --json-schema в команде call_text отсутствует."""

    stdout = _stdout_with_events(_result_event("текст", session_id="s1"))
    mock_run = Mock(return_value=_proc(stdout))
    monkeypatch.setattr(subprocess, "run", mock_run)

    call_text(
        "инструкция", "текст акта",
        run_id="run_text4", trace_name="02_summary.raw.json", allowed_tools=[],
    )

    cmd = mock_run.call_args_list[0].args[0]
    assert "--json-schema" not in cmd
    # Инструкция всё равно должна доехать до команды как есть
    assert _extract_message_text(cmd) == "инструкция"


def test_call_text_raw_saved_every_attempt(tmp_runs_dir, monkeypatch):
    """18. Сырой ответ call_text сохраняется на каждой попытке, включая пустые/упавшие."""

    stdouts = [
        _stdout_with_events(_result_event("", session_id="s1")),
        _stdout_with_events(_result_event("", session_id="s1")),
        _stdout_with_events(_result_event("текст", session_id="s1")),
    ]
    mock_run = Mock(side_effect=[_proc(s) for s in stdouts])
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call_text(
        "инструкция", "текст акта",
        run_id="run_text5", trace_name="02_summary.raw.json", allowed_tools=[],
        max_attempts=3,
    )

    assert result.attempts == 3
    saved = sorted((tmp_runs_dir / "run_text5").glob("02_summary.raw*.json"))
    assert len(saved) == 3
    contents = {p.read_text(encoding="utf-8") for p in saved}
    assert contents == set(stdouts)


# ---- Живой тест ----


class _LiveAnswer(BaseModel):
    """Минимальная схема для живого теста — не связана с доменными контрактами."""

    model_config = ConfigDict(extra="forbid")

    value: int


@pytest.mark.live
def test_live_simple_call(tmp_runs_dir):
    """11. Живой вызов настоящего claude -p с простой схемой: ok=True."""
    result: CallResult[_LiveAnswer] = call(
        "Верни JSON с единственным полем value, равным числу 42. "
        "Не добавляй никакого текста и пояснений вне JSON.",
        None,
        _LiveAnswer,
        run_id="live_run",
        trace_name="99_live/01.raw.json",
        allowed_tools=[],
        max_attempts=2,
        timeout_s=120,
    )

    assert result.ok is True
    assert result.value is not None
    assert result.value.value == 42

# ----------------------------------------------------------------------
# Модель задаётся явно. Без флагов --model/--effort CLI берёт модель из
# ~/.claude/settings.json машины — движок сервиса зависел бы от чужого
# файла, и правка чужих настроек молча меняла бы качество вердиктов.
# ----------------------------------------------------------------------


def test_model_and_effort_always_in_command():
    """Флаги модели и уровня рассуждений уходят в каждый вызов."""
    from app.config import EFFORT, MODEL

    cmd = _build_cmd("текст", "{}", [], None, None)
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == MODEL
    assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == EFFORT


def test_model_and_effort_present_on_resume_retry():
    """На ретрае через --resume политика модели передаётся заново."""
    from app.config import EFFORT, MODEL

    cmd = _build_cmd("исправь", "{}", ["WebSearch"], "sess-1", None)
    assert "--resume" in cmd
    assert cmd[cmd.index("--model") + 1] == MODEL
    assert cmd[cmd.index("--effort") + 1] == EFFORT


# Изоляция вызова. Проверено 2026-09-25 (план итерации 3, шаг 0): с одним
# --allowedTools агенту сервиса доступны 75 инструментов, включая Bash, Write,
# Edit и 49 инструментов Gmail / Google Drive / Claude Docs, а Bash выполняется
# без запроса разрешения — ~/.claude/settings.local.json спарка разрешает его
# везде, а --allowedTools набор не ограничивает, только одобряет. В каждый вызов
# подмешивался и личный ~/.claude/CLAUDE.md владельца.
# ----------------------------------------------------------------------


def test_tools_restricted_to_node_list():
    """--tools = ровно инструменты узла: пусто для текстовых, два для поисковых."""
    cmd = _build_cmd("текст", "{}", [], None, None)
    assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""

    cmd = _build_cmd("текст", "{}", ["WebSearch", "WebFetch"], None, None)
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch"
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch WebFetch"


def test_isolation_flags_in_every_call():
    """MCP-серверы, CLAUDE.md и запросы разрешений отключены в каждом вызове, и на ретрае тоже."""
    for cmd in (
        _build_cmd("текст", "{}", [], None, None),
        _build_cmd("исправь", "{}", ["WebSearch"], "sess-1", None),
        _build_cmd("текст", None, [], None, 1.0),
    ):
        assert "--strict-mcp-config" in cmd
        assert "--safe-mode" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"


def test_model_is_sonnet_medium():
    """Согласовано с владельцем 2026-08-25: sonnet, medium."""
    from app.config import EFFORT, MODEL

    assert MODEL == "sonnet"
    assert EFFORT == "medium"
