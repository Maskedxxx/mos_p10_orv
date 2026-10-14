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

import sys
import threading
import time

import app.runner as runner_mod
from app.contracts import Verdict
from app.runner import CallResult, _build_cmd, call, call_text
from app.runner import _exec as _REAL_EXEC   # настоящий построчный запуск — до подмены фикстурой ниже


@pytest.fixture(autouse=True)
def _exec_via_subprocess_run(request, monkeypatch):
    """
    Переходник для тестов цикла попыток: запуск процесса (_exec, построчное чтение stream-json)
    подменяется вызовом subprocess.run с теми же аргументами, что были до 2.7, — тесты подменяют
    subprocess.run и проверяют команду, окружение, документ и таймаут. Сам _exec проверяется
    на настоящем процессе — тесты «_exec» внизу файла; живой тест идёт через настоящий _exec.
    """
    if request.node.get_closest_marker("live"):
        return

    def fake(cmd, *, timeout_s, env, document, stream_path, attempt):
        kwargs = {"capture_output": True, "text": True, "timeout": timeout_s}
        if env is not None:
            kwargs["env"] = env
        if document is not None:
            kwargs["input"] = document
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        return subprocess.run(cmd, **kwargs)
    monkeypatch.setattr(runner_mod, "_exec", fake)


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
    """8. Стоимость суммируется по всем попыткам. Вторая попытка идёт через --resume сессии s1, а CLI при --resume
    сообщает стоимость сессии с начала (14.10): 0.05 = 0.02 первой попытки + 0.03 второй."""

    bad_stdout = _stdout_with_events(_result_event("{плохой", session_id="s1", cost=0.02))
    good_stdout = _stdout_with_events(
        _result_event(json.dumps(valid_verdict_ok), session_id="s1", cost=0.05)
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


def test_build_cmd_model_override():
    """Модель вызова задаётся параметром (оркестратор диалога — DIALOG_MODEL), по умолчанию — MODEL."""
    from app.config import MODEL
    assert _build_cmd("т", "{}", [], None, None)[_build_cmd("т", "{}", [], None, None).index("--model") + 1] == MODEL
    cmd = _build_cmd("т", "{}", [], None, None, model="opus")
    assert cmd[cmd.index("--model") + 1] == "opus"



# ---- 2.6: кэш — общий контекст в системный промпт, срок кэша 5 минут для пайплайна ----


def _ok_stdout(valid: dict) -> str:
    return _stdout_with_events(_result_event(json.dumps(valid)))


def _flag_value(cmd: list[str], flag: str) -> str | None:
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def test_shared_context_goes_to_system_prompt_file(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """Акт и паспорт — общий для группы агентов текст — уходят файлом в системный промпт:
    кэш берёт куски целиком, и только так агенты с разными заданиями читают акт друг у друга."""
    mock_run = Mock(return_value=_proc(_ok_stdout(valid_verdict_ok)))
    monkeypatch.setattr(subprocess, "run", mock_run)
    call("инструкция", None, Verdict, run_id="run_ctx", trace_name="03_verdicts/01.raw.json",
         allowed_tools=[], shared_context="## ПОЛНЫЙ ТЕКСТ АКТА\n\nакт")
    cmd, kwargs = mock_run.call_args.args[0], mock_run.call_args.kwargs
    path = _flag_value(cmd, "--system-prompt-file")
    assert path is not None and path.startswith(str(tmp_runs_dir / "run_ctx" / "_context"))
    from app.config import SERVICE_SYSTEM_PROMPT
    assert open(path, encoding="utf-8").read() == f"{SERVICE_SYSTEM_PROMPT}\n\n## ПОЛНЫЙ ТЕКСТ АКТА\n\nакт"
    assert "--append-system-prompt-file" not in cmd   # 14.10: стандартный промпт Claude Code не используется
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_same_context_is_one_file(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    mock_run = Mock(return_value=_proc(_ok_stdout(valid_verdict_ok)))
    monkeypatch.setattr(subprocess, "run", mock_run)
    for n in (1, 2):
        call(f"инструкция {n}", None, Verdict, run_id="run_ctx", trace_name=f"03_verdicts/0{n}.raw.json",
             allowed_tools=[], shared_context="акт")
    paths = {_flag_value(c.args[0], "--system-prompt-file") for c in mock_run.call_args_list}
    assert len(paths) == 1 and len(list((tmp_runs_dir / "run_ctx" / "_context").iterdir())) == 1


def test_pipeline_cache_ttl_is_five_minutes_dialog_keeps_default(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """Пайплайн — срок кэша 5 минут (запись 1,25× вместо 2×); диалог — как было (1 час)."""
    mock_run = Mock(return_value=_proc(_ok_stdout(valid_verdict_ok)))
    monkeypatch.setattr(subprocess, "run", mock_run)
    call("и", "док", Verdict, run_id="run_ttl", trace_name="a.raw.json", allowed_tools=[])
    assert mock_run.call_args.kwargs["env"]["FORCE_PROMPT_CACHING_5M"] == "1"
    call("и", "док", Verdict, run_id="run_ttl", trace_name="b.raw.json", allowed_tools=[], short_cache_ttl=False)
    assert "FORCE_PROMPT_CACHING_5M" not in (mock_run.call_args.kwargs.get("env") or {})


def test_without_shared_context_command_unchanged(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    mock_run = Mock(return_value=_proc(_ok_stdout(valid_verdict_ok)))
    monkeypatch.setattr(subprocess, "run", mock_run)
    call("и", "док", Verdict, run_id="run_plain", trace_name="a.raw.json", allowed_tools=[])
    from app.config import SERVICE_SYSTEM_PROMPT
    assert _flag_value(mock_run.call_args.args[0], "--system-prompt") == SERVICE_SYSTEM_PROMPT   # 14.10: свой промпт
    assert "--system-prompt-file" not in mock_run.call_args.args[0]
    assert mock_run.call_args.kwargs["input"] == "док"


def test_resume_keeps_shared_context(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """Повтор через --resume после ответа не по схеме — с тем же системным промптом."""
    bad = _stdout_with_events(_result_event("{\"verdict\": \"x\"}", session_id="sess-9"))
    mock_run = Mock(side_effect=[_proc(bad), _proc(_ok_stdout(valid_verdict_ok))])
    monkeypatch.setattr(subprocess, "run", mock_run)
    res = call("и", None, Verdict, run_id="run_res", trace_name="a.raw.json", allowed_tools=[], shared_context="акт")
    second = mock_run.call_args_list[1].args[0]
    assert res.ok and "--resume" in second and _flag_value(second, "--system-prompt-file")


# ---- 2.7 (итерация 4, Б): Гарант через MCP ----
# Проба 2026-10-12 на спарке (CLI 2.1.292): --safe-mode отключает MCP-серверы
# целиком (mcp_servers: []), поэтому агенту с Гарантом изоляция собирается иначе:
# --setting-sources local (без личного CLAUDE.md и плагинов машины),
# --disable-slash-commands (без навыков), --strict-mcp-config + --mcp-config
# (только сервер Гаранта); прокси, которые жили в настройках машины, — через
# окружение процесса. Проверено: сервер connected, CLAUDE.md в контексте нет.


@pytest.fixture
def garant_files(tmp_path, monkeypatch):
    """Токен и настройки CLI машины — во временных файлах, чтобы тест не читал настоящие."""
    import app.runner as runner_mod
    token = tmp_path / "token"
    token.write_text("SECRET-TOKEN-123\n", encoding="utf-8")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"HTTPS_PROXY": "http://p:1", "HTTP_PROXY": "http://p:1",
                                            "NO_PROXY": "localhost", "DISABLE_AUTOUPDATER": "1"},
                                    "model": "opus"}), encoding="utf-8")
    monkeypatch.setattr(runner_mod, "GARANT_TOKEN_FILE", token)
    monkeypatch.setattr(runner_mod, "CLI_SETTINGS_FILE", settings)
    return token


def test_mcp_tools_command_flags():
    """С инструментами Гаранта: сервер Гаранта, только перечисленные его инструменты, без --safe-mode."""
    from app.config import GARANT_MCP_CONFIG
    cmd = _build_cmd("т", "{}", ["WebSearch"], None, None, mcp_tools=["garant_search", "garant_snippets"])
    assert _flag_value(cmd, "--mcp-config") == str(GARANT_MCP_CONFIG)
    assert "--strict-mcp-config" in cmd and "--safe-mode" not in cmd
    assert _flag_value(cmd, "--setting-sources") == ""
    assert "--disable-slash-commands" in cmd
    assert _flag_value(cmd, "--permission-mode") == "dontAsk"
    assert _flag_value(cmd, "--tools") == "WebSearch"   # встроенные — как раньше
    assert _flag_value(cmd, "--allowedTools") == "WebSearch mcp__garant__garant_search mcp__garant__garant_snippets"
    # 14.10: остальные инструменты Гаранта скрыты от модели
    from app.config import GARANT_ALL_TOOLS
    denied = _flag_value(cmd, "--disallowedTools").split()
    assert len(denied) == len(GARANT_ALL_TOOLS) - 2
    assert "mcp__garant__garant_export_html" in denied and "mcp__garant__garant_search" not in denied


def test_without_mcp_tools_isolation_unchanged():
    """Без Гаранта команда прежняя (до 2.7): та же изоляция, тот же порядок флагов; формат вывода — построчный
    (stream-json, мониторинг 2.7), ответ модели — кусками (первый ответ целиком, 13.10); 14.10 — свой системный
    промпт вместо стандартного."""
    from app.config import EFFORT, MODEL, SERVICE_SYSTEM_PROMPT
    expected = ["claude", "-p", "т", "--json-schema", "{}", "--include-partial-messages",
                "--output-format", "stream-json", "--verbose",
                "--tools", "WebSearch",
                "--allowedTools", "WebSearch", "--strict-mcp-config", "--safe-mode", "--permission-mode", "dontAsk",
                "--model", MODEL, "--effort", EFFORT, "--system-prompt", SERVICE_SYSTEM_PROMPT]
    assert _build_cmd("т", "{}", ["WebSearch"], None, None) == expected
    assert _build_cmd("т", "{}", ["WebSearch"], None, None, mcp_tools=[]) == expected


def test_garant_mcp_config_has_no_token():
    """Файл подключения лежит в репо: токена в нём нет, только подстановка из окружения."""
    from app.config import GARANT_MCP_CONFIG
    data = json.loads(GARANT_MCP_CONFIG.read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"garant"}   # второй сервер в этом файле подключился бы агентам
    server = data["mcpServers"]["garant"]
    assert server["url"] == "https://mcp.garant.ru/mcp"
    assert server["headers"]["Authorization"] == "Bearer ${GARANT_TOKEN}"


def test_call_with_mcp_passes_token_and_proxy_only_in_env(tmp_runs_dir, valid_verdict_ok, garant_files, monkeypatch, caplog):
    """Токен — только в окружении процесса этого вызова; в команде, логе и трейсе его нет."""
    mock_run = Mock(return_value=_proc(_ok_stdout(valid_verdict_ok)))
    monkeypatch.setattr(subprocess, "run", mock_run)
    caplog.set_level("DEBUG")
    res = call("и", "док", Verdict, run_id="run_g", trace_name="03_verdicts/03.raw.json",
               allowed_tools=["WebSearch"], mcp_tools=["garant_search"])
    assert res.ok
    env = mock_run.call_args.kwargs["env"]
    assert env["GARANT_TOKEN"] == "SECRET-TOKEN-123"
    assert env["HTTPS_PROXY"] == "http://p:1"
    assert env["NO_PROXY"] == "localhost,mcp.garant.ru"   # Гарант — мимо прокси, модель — через него
    assert env["MAX_MCP_OUTPUT_TOKENS"] == "100000"       # большой ответ Гаранта — агенту, а не в файл
    assert env["FORCE_PROMPT_CACHING_5M"] == "1"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert "SECRET-TOKEN-123" not in " ".join(mock_run.call_args.args[0])
    assert "SECRET-TOKEN-123" not in caplog.text
    for f in (tmp_runs_dir / "run_g").rglob("*"):
        if f.is_file():
            assert "SECRET-TOKEN-123" not in f.read_text(encoding="utf-8", errors="ignore")


def test_call_without_mcp_has_no_token_in_env(tmp_runs_dir, valid_verdict_ok, garant_files, monkeypatch):
    """Без Гаранта токена нет в окружении — и на основном пути пайплайна (кэш 5 минут), и в диалоге."""
    mock_run = Mock(return_value=_proc(_ok_stdout(valid_verdict_ok)))
    monkeypatch.setattr(subprocess, "run", mock_run)
    call("и", "док", Verdict, run_id="run_ng", trace_name="a.raw.json", allowed_tools=[])
    env = mock_run.call_args.kwargs["env"]
    assert "GARANT_TOKEN" not in env and "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in env
    call("и", "док", Verdict, run_id="run_ng", trace_name="b.raw.json", allowed_tools=[], short_cache_ttl=False)
    assert "GARANT_TOKEN" not in (mock_run.call_args.kwargs.get("env") or {})


def test_retries_keep_garant_flags_and_env(tmp_runs_dir, valid_verdict_ok, garant_files, monkeypatch):
    """Ретрай через --resume и полный повтор после сбоя — с теми же флагами Гаранта и окружением; токена нет в ошибке."""
    bad = _stdout_with_events(_result_event("{\"verdict\": \"x\"}", session_id="sess-9"))
    mock_run = Mock(side_effect=[_proc(bad), _proc("", returncode=1, stderr="сбой"), _proc(_ok_stdout(valid_verdict_ok))])
    monkeypatch.setattr(subprocess, "run", mock_run)
    res = call("и", "док", Verdict, run_id="run_rt", trace_name="a.raw.json", allowed_tools=[], mcp_tools=["garant_search"])
    assert res.ok and len(mock_run.call_args_list) == 3
    for c in mock_run.call_args_list:
        assert _flag_value(c.args[0], "--mcp-config") and "mcp__garant__garant_search" in _flag_value(c.args[0], "--allowedTools")
        assert c.kwargs["env"]["GARANT_TOKEN"] == "SECRET-TOKEN-123"
    assert "--resume" in mock_run.call_args_list[1].args[0]
    assert "SECRET-TOKEN-123" not in (res.error or "")


@pytest.mark.parametrize("content", ["", "  \n", "AB CD"])
def test_bad_token_file_fails_clearly(tmp_runs_dir, tmp_path, monkeypatch, content):
    """Пустой или испорченный файл токена — понятная ошибка до запуска модели."""
    import app.runner as runner_mod
    token = tmp_path / "token"
    token.write_text(content, encoding="utf-8")
    monkeypatch.setattr(runner_mod, "GARANT_TOKEN_FILE", token)
    monkeypatch.setattr(subprocess, "run", Mock())
    with pytest.raises(RuntimeError, match="токеном Гаранта"):
        call("и", "док", Verdict, run_id="run_bt", trace_name="a.raw.json", allowed_tools=[], mcp_tools=["garant_search"])


@pytest.mark.parametrize("settings", ["не json", "[]", '{"env": [1, 2]}'])
def test_broken_cli_settings_do_not_break_call(tmp_runs_dir, valid_verdict_ok, garant_files, monkeypatch, settings):
    """Испорченные настройки CLI не роняют вызов: просто без прокси."""
    import app.runner as runner_mod
    runner_mod.CLI_SETTINGS_FILE.write_text(settings, encoding="utf-8")
    mock_run = Mock(return_value=_proc(_ok_stdout(valid_verdict_ok)))
    monkeypatch.setattr(subprocess, "run", mock_run)
    assert call("и", "док", Verdict, run_id="run_bs", trace_name="a.raw.json", allowed_tools=[],
                mcp_tools=["garant_search"]).ok
    assert "HTTPS_PROXY" not in {k for k in mock_run.call_args.kwargs["env"] if mock_run.call_args.kwargs["env"].get(k) == "http://p:1"}
    assert mock_run.call_args.kwargs["env"]["NO_PROXY"] == "mcp.garant.ru"


def test_garant_host_bypasses_proxy_matches_config():
    """Хост, который идёт мимо прокси, — тот же, что в файле подключения к Гаранту."""
    from urllib.parse import urlparse
    from app.config import GARANT_MCP_CONFIG, GARANT_NO_PROXY_HOST
    url = json.loads(GARANT_MCP_CONFIG.read_text(encoding="utf-8"))["mcpServers"]["garant"]["url"]
    assert urlparse(url).hostname == GARANT_NO_PROXY_HOST


def test_call_with_mcp_without_token_file_fails_clearly(tmp_runs_dir, valid_verdict_ok, tmp_path, monkeypatch):
    """Нет файла токена — понятная ошибка до запуска модели, а не тихий вызов без Гаранта."""
    import app.runner as runner_mod
    monkeypatch.setattr(runner_mod, "GARANT_TOKEN_FILE", tmp_path / "нет")
    monkeypatch.setattr(subprocess, "run", Mock())
    with pytest.raises(RuntimeError, match="токеном Гаранта"):
        call("и", "док", Verdict, run_id="run_nt", trace_name="a.raw.json", allowed_tools=[], mcp_tools=["garant_search"])



# ---- Журнал сессии: вызовы агентов (2.7, мониторинг) ----


def test_call_logs_agent_start_attempts_and_finish(tmp_runs_dir, valid_verdict_ok, garant_files, monkeypatch):
    """Вызов агента в журнале сессии: старт (модель, инструменты, Гарант), каждая попытка с исходом, итог."""
    from app.trace import new_run, read_events
    rid = new_run("акт.docx")
    bad = _stdout_with_events(_result_event("{\"verdict\": \"x\"}", session_id="sess-9", cost=0.02, num_turns=4))
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=[_proc(bad), _proc(_ok_stdout(valid_verdict_ok))]))
    call("и", "док", Verdict, run_id=rid, trace_name="03_verdicts/03.raw.json",
         allowed_tools=["WebSearch"], mcp_tools=["garant_search"], timeout_s=900)
    ev = [e for e in read_events(rid) if e["event"].startswith("agent_")]
    assert [e["event"] for e in ev] == ["agent_started", "agent_attempt", "agent_attempt", "agent_finished"]
    assert all(e["who"] == "агент 03_verdicts/03" for e in ev)
    start = ev[0]
    assert start["tools"] == ["WebSearch"] and start["garant"] == ["garant_search"] and start["timeout_s"] == 900
    assert [(e["attempt"], e["outcome"]) for e in ev[1:3]] == [(1, "validation_error"), (2, "ok")]
    assert ev[1]["cost_usd"] == 0.02 and ev[1]["turns"] == 4 and ev[1]["error"]
    assert ev[3]["ok"] is True and ev[3]["attempts"] == 2


def test_call_text_logs_agent_events(tmp_runs_dir, monkeypatch):
    """Текстовый вызов (диалог) — те же события агента."""
    from app.trace import new_run, read_events
    rid = new_run("акт.docx")
    monkeypatch.setattr(subprocess, "run", Mock(return_value=_proc(_stdout_with_events(_result_event("ответ")))))
    call_text("и", None, run_id=rid, trace_name="dialog/turns/01.raw.json", allowed_tools=[])
    kinds = [e["event"] for e in read_events(rid) if e["event"].startswith("agent_")]
    assert kinds == ["agent_started", "agent_attempt", "agent_finished"]



# ---- _exec: ход агента построчно (2.7, мониторинг, шаг 2) ----
# Вместо claude — маленький скрипт на Python: печатает события stream-json с паузами,
# читает stdin, умеет зависать. Так проверяется настоящий процесс, без модели.

_FAKE_AGENT = r"""
import json, sys, time
mode = sys.argv[1]
doc = sys.stdin.read() if mode != "nostdin" else ""
def out(e):
    print(json.dumps(e, ensure_ascii=False), flush=True)
out({"type": "system", "subtype": "init", "doc_len": len(doc)})
if mode == "hang":
    time.sleep(30)
if mode == "garbage":
    print("это не JSON", flush=True)
time.sleep(0.4)
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__garant__garant_search"}]}})
time.sleep(0.4)
out({"type": "result", "subtype": "success", "result": "{}", "total_cost_usd": 0.01})
print("предупреждение", file=sys.stderr)
sys.exit(3 if mode == "fail" else 0)
"""


def _fake_cmd(mode: str) -> list[str]:
    return [sys.executable, "-c", _FAKE_AGENT, mode]


def test_exec_writes_each_event_while_agent_works(tmp_path):
    """Событие попадает в файл хода сразу, пока агент ещё работает; в конце stdout — тот же список событий,
    что был при --output-format json (итоговый .raw.json не меняется)."""
    stream = tmp_path / "03.stream.jsonl"
    box = {}
    worker = threading.Thread(target=lambda: box.update(proc=_REAL_EXEC(
        _fake_cmd("ok"), timeout_s=30, env=None, document="акт", stream_path=stream, attempt=1)))
    worker.start()
    deadline = time.monotonic() + 10
    while not (stream.is_file() and stream.read_text(encoding="utf-8").strip()) and time.monotonic() < deadline:
        time.sleep(0.05)
    seen_live = len(stream.read_text(encoding="utf-8").splitlines())
    worker.join()
    assert 1 <= seen_live < 3                       # первое событие видно до конца работы агента
    proc = box["proc"]
    events = json.loads(proc.stdout)
    assert [e["type"] for e in events] == ["system", "assistant", "result"]
    assert events[0]["doc_len"] == len("акт")       # документ дошёл через stdin
    lines = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()]
    assert [ln["event"]["type"] for ln in lines] == ["system", "assistant", "result"]
    assert all(ln["attempt"] == 1 and ln["ts"] for ln in lines)
    assert proc.returncode == 0 and "предупреждение" in proc.stderr


def test_exec_timeout_keeps_partial_trace(tmp_path):
    """Зависший агент убивается по таймауту; что он успел сделать — и в файле хода, и в TimeoutExpired."""
    stream = tmp_path / "03.stream.jsonl"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        _REAL_EXEC(_fake_cmd("hang"), timeout_s=1, env=None, document=None, stream_path=stream, attempt=2)
    assert time.monotonic() - started < 10
    partial = json.loads(exc_info.value.output)
    assert [e["type"] for e in partial] == ["system"]
    lines = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["attempt"] == 2 and lines[0]["event"]["type"] == "system"


def test_exec_appends_attempts_to_one_file(tmp_path):
    """Повторная попытка того же агента дописывается в тот же файл хода со своим номером."""
    stream = tmp_path / "03.stream.jsonl"
    _REAL_EXEC(_fake_cmd("ok"), timeout_s=30, env=None, document="", stream_path=stream, attempt=1)
    _REAL_EXEC(_fake_cmd("ok"), timeout_s=30, env=None, document="", stream_path=stream, attempt=2)
    attempts = [json.loads(line)["attempt"] for line in stream.read_text(encoding="utf-8").splitlines()]
    assert attempts == [1, 1, 1, 2, 2, 2]


def test_exec_non_json_line_returns_raw_text(tmp_path):
    """Строка не JSON — stdout отдаётся как есть: цикл попыток увидит «вывод не разбирается», как раньше."""
    stream = tmp_path / "s.jsonl"
    proc = _REAL_EXEC(_fake_cmd("garbage"), timeout_s=30, env=None, document="", stream_path=stream, attempt=1)
    assert "это не JSON" in proc.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(proc.stdout)
    assert any(json.loads(line).get("text") == "это не JSON" for line in stream.read_text(encoding="utf-8").splitlines())


def test_exec_passes_return_code(tmp_path):
    """Код возврата процесса — как есть."""
    proc = _REAL_EXEC(_fake_cmd("fail"), timeout_s=30, env=None, document="", stream_path=tmp_path / "s.jsonl",
                      attempt=1)
    assert proc.returncode == 3


def test_call_streams_to_file_next_to_raw(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """Цикл попыток передаёт _exec файл хода рядом с сырым ответом (03.stream.jsonl) и номер попытки;
    команда — stream-json."""
    seen = []
    bad = _stdout_with_events(_result_event("не json", session_id="s-1"))

    def fake(cmd, *, timeout_s, env, document, stream_path, attempt):
        seen.append((cmd, stream_path, attempt))
        return _proc(bad if attempt == 1 else _ok_stdout(valid_verdict_ok))
    monkeypatch.setattr(runner_mod, "_exec", fake)
    assert call("и", "док", Verdict, run_id="run_st", trace_name="03_verdicts/03.raw.json", allowed_tools=[]).ok
    assert [a for _, _, a in seen] == [1, 2]
    assert all(p == tmp_runs_dir / "run_st" / "03_verdicts" / "03.stream.jsonl" for _, p, _ in seen)
    cmd = seen[0][0]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd



# ---- Первый ответ модели + починка JSON по схеме (13.10, разбор паспорта) ----


def _structured_answer(text: str, index: int = 0) -> list[dict[str, Any]]:
    """Частичные сообщения CLI (--include-partial-messages): ответ модели в StructuredOutput кусками."""
    def ev(e: dict[str, Any]) -> dict[str, Any]:
        return {"type": "stream_event", "event": e}
    half = len(text) // 2
    return [
        ev({"type": "content_block_start", "index": index,
            "content_block": {"type": "tool_use", "name": "StructuredOutput", "input": {}}}),
        ev({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": text[:half]}}),
        ev({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": text[half:]}}),
        ev({"type": "content_block_stop", "index": index}),
    ]


def test_structured_calls_stream_partial_messages():
    """Вызов со схемой пишет ответ модели кусками — иначе первый (отклонённый CLI) ответ не сохранить."""
    assert "--include-partial-messages" in _build_cmd("и", "{}", [], None, None)
    assert "--include-partial-messages" not in _build_cmd("и", None, [], None, None)


def test_first_answer_is_taken_even_if_cli_accepted_another(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """Берётся ПЕРВЫЙ ответ модели, а не принятый CLI после переписывания (тот бывает урезан)."""
    first = dict(valid_verdict_ok, rationale="полный первый ответ")
    later = dict(valid_verdict_ok, rationale="урезанный")
    stdout = _stdout_with_events(*_structured_answer(json.dumps(first, ensure_ascii=False)),
                                 _result_event(json.dumps(later, ensure_ascii=False)))
    monkeypatch.setattr(subprocess, "run", Mock(return_value=_proc(stdout)))

    result = call("инструкция", "текст акта", Verdict,
                  run_id="run1", trace_name="02_passport.raw.json", allowed_tools=[])

    assert result.ok is True and result.attempts == 1
    assert result.value.rationale == "полный первый ответ"


def test_broken_first_answer_is_repaired_by_schema(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """Первый ответ без последней скобки чинится санитайзером по схеме — без повтора; в журнале json_repaired."""
    from app.trace import read_events, new_run
    rid = new_run("акт.docx")
    broken = json.dumps(valid_verdict_ok, ensure_ascii=False)[:-1]
    stdout = _stdout_with_events(*_structured_answer(broken), _result_event("{}"))
    mock_run = Mock(return_value=_proc(stdout))
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = call("инструкция", "текст акта", Verdict,
                  run_id=rid, trace_name="02_passport.raw.json", allowed_tools=[])

    assert result.ok is True and result.attempts == 1 and mock_run.call_count == 1
    assert result.value == Verdict.model_validate(valid_verdict_ok)
    assert [e for e in read_events(rid) if e["event"] == "json_repaired"]


def test_own_mcp_config_and_full_tool_names():
    """Помощник (14.10): свой файл серверов вместо файла Гаранта; полные имена «mcp__…» одобряются как есть,
    не прячутся --disallowedTools; неразрешённые инструменты Гаранта по-прежнему скрыты."""
    cmd = _build_cmd("т", "{}", ["WebSearch"], None, None,
                     mcp_tools=["garant_search", "mcp__p10run__read_artifact"], mcp_config="/r/dialog/mcp.json")
    assert _flag_value(cmd, "--mcp-config") == "/r/dialog/mcp.json"
    assert _flag_value(cmd, "--allowedTools") == "WebSearch mcp__garant__garant_search mcp__p10run__read_artifact"
    denied = _flag_value(cmd, "--disallowedTools").split()
    assert "mcp__p10run__read_artifact" not in denied and "mcp__garant__garant_search" not in denied
    assert "mcp__garant__garant_export_html" in denied
    assert _flag_value(cmd, "--setting-sources") == "" and "--safe-mode" not in cmd


def test_resume_session_first_attempt_without_document(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """resume_session_id: первая попытка — --resume этой сессии, документ в stdin не идёт."""
    mock_run = Mock(return_value=_proc(_ok_stdout(valid_verdict_ok)))
    monkeypatch.setattr(subprocess, "run", mock_run)
    assert call("вопрос", "док", Verdict, run_id="run_rs", trace_name="a.raw.json", allowed_tools=[],
                resume_session_id="sess-dlg").ok
    cmd = mock_run.call_args.args[0]
    assert _flag_value(cmd, "--resume") == "sess-dlg" and "вопрос" in cmd
    assert "input" not in mock_run.call_args.kwargs


def test_resume_session_kept_after_process_failure(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """Сбой процесса при продолжении разговора — повтор тоже через --resume той же сессии, а не с нуля."""
    mock_run = Mock(side_effect=[_proc("", returncode=1, stderr="сбой"), _proc(_ok_stdout(valid_verdict_ok))])
    monkeypatch.setattr(subprocess, "run", mock_run)
    assert call("вопрос", "док", Verdict, run_id="run_rs2", trace_name="a.raw.json", allowed_tools=[],
                resume_session_id="sess-dlg").ok
    for c in mock_run.call_args_list:
        assert _flag_value(c.args[0], "--resume") == "sess-dlg"


def test_resume_cost_counts_only_this_run(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """14.10: при --resume CLI сообщает стоимость сессии с начала — расход хода = сообщённое минус прежнее."""
    mock_run = Mock(return_value=_proc(_stdout_with_events(_result_event(json.dumps(valid_verdict_ok, ensure_ascii=False),
                                                                          session_id="sess-dlg", cost=0.58))))
    monkeypatch.setattr(subprocess, "run", mock_run)
    res = call("вопрос", None, Verdict, run_id="run_rc", trace_name="a.raw.json", allowed_tools=[],
               resume_session_id="sess-dlg", resume_cost_base=0.52)
    assert res.cost_usd == pytest.approx(0.06) and res.session_cost_usd == pytest.approx(0.58)


def test_validation_retry_via_resume_not_double_counted(tmp_runs_dir, valid_verdict_ok, monkeypatch):
    """Ретрай через --resume внутри вызова: вторая попытка сообщает сумму двух — считается только её часть."""
    bad = _stdout_with_events(_result_event("{\"verdict\": \"x\"}", session_id="sess-9", cost=0.10))
    good = _stdout_with_events(_result_event(json.dumps(valid_verdict_ok, ensure_ascii=False), session_id="sess-9", cost=0.15))
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=[_proc(bad), _proc(good)]))
    res = call("и", "док", Verdict, run_id="run_rc2", trace_name="a.raw.json", allowed_tools=[])
    assert res.ok and res.cost_usd == pytest.approx(0.15)
