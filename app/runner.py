"""
Обёртка над `claude -p` — единая точка запуска модели для узлов пайплайна П-10.

Берёт на себя: сборку команды, передачу документа через stdin, разбор вывода
`--output-format json`, валидацию ответа и ретраи при сбоях. Про доменную логику
(что делать с провалившимся результатом) ничего не знает — просто возвращает
CallResult(ok=False, ...), решение остаётся за вызывающим узлом (например узел
вердиктов сам соберёт Verdict со статусом "ошибка").

Два режима вызова:
    - call()       — узлу нужен строго типизированный JSON-ответ по pydantic-модели
                      (передаёт `--json-schema`, валидирует result через model_cls).
    - call_text()   — узлу нужен свободный текст/markdown, а не JSON (саммари и
                      т.п.). Без `--json-schema` и без модели, value — строка.

Виды сбоя процесса/API и стратегия ретрая — ОБЩИЕ для call() и call_text() (см.
_run_attempt): все ретраятся полным повтором, БЕЗ --resume, документ шлётся заново:
    - таймаут процесса
    - процесс упал (returncode != 0)
    - stdout не разбирается как JSON
    - нет события type == "result"
    - is_error: true (ошибка API)

Дальше пути расходятся по содержательной проверке самого поля result:
    - call(): result не разбирается как JSON, либо не проходит pydantic-валидацию
      -> ретрай ЧЕРЕЗ --resume, с текстом ошибки (сессия жива, документ повторно
      не шлём — модель его уже видела).
    - call_text(): содержательной проверки нет, кроме одной — пустой/пробельный
      ответ считается неудачей и ретраится ПОЛНЫМ повтором, без --resume (в этом
      режиме --resume не используется вообще).

Логика call(): если сорвался сам процесс или API — начинаем заново (документ шлём
повторно, сессии может не быть). Если модель ответила, но JSON плохой — продолжаем
диалог через --resume, документ повторно не шлём.

Сырой stdout сохраняется на КАЖДОЙ попытке через trace.save_raw (в обоих режимах) —
прямое требование владельца: без сырых ответов нечем отлаживать сбои модели.

Бюджет — два независимых уровня защиты: max_budget_usd передаётся в саму CLI
как --max-budget-usd (потолок на ОДИН вызов процесса — останавливает
зациклившийся вызов до того, как он потратит много) и отдельно проверяется в
Python как сумма total_cost_usd по всем попыткам (потолок на весь call()/call_text()).

permission_denials собираются по ВСЕМ попыткам в CallResult.permission_denials —
непустой список означает, что промпту узла зачем-то понадобился инструмент,
на который не было прав (сигнал кривого промпта, особенно у текстовых
индикаторов). Кроме попадания в CallResult, это же самое пишется в лог
отдельной предупреждающей строкой при получении события.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import EFFORT, MODEL
from app.logger import get_run_logger
from app.trace import save_raw

# Тип модели ответа для call() — только pydantic-модели (--json-schema требует схему)
ModelT = TypeVar("ModelT", bound=BaseModel)

# Тип значения в CallResult — шире, чем ModelT: call() кладёт pydantic-модель,
# call_text() кладёт обычную строку. Без bound=BaseModel, иначе CallResult[str]
# был бы некорректен по типам.
ValueT = TypeVar("ValueT")


@dataclass
class CallResult(Generic[ValueT]):
    """Итог вызова claude -p: провалидированный ответ (или строка) либо признак неудачи."""

    value: ValueT | None
    ok: bool
    attempts: int
    cost_usd: float
    session_id: str | None
    error: str | None
    permission_denials: list[Any]
    duration_s: float


@dataclass
class _AttemptOutcome:
    """
    Итог одного запуска процесса claude -p — общая часть между call() и call_text().

    kind == "ok": процесс/API отработали штатно, result_text содержит поле result
    события (разбором и валидацией самого содержимого result_text занимается уже
    вызывающая функция — call() или call_text() — у них разные требования).

    Любой другой kind ("timeout" | "process_error" | "top_level_json_error" |
    "no_result_event" | "api_error") — сбой процесса/API. Для call() и call_text()
    он обрабатывается ОДИНАКОВО: полный повтор без --resume.
    """

    kind: str
    error: str | None
    result_text: str | None
    session_id: str | None
    cost_usd: float
    num_turns: int | None
    permission_denials: list[Any]
    duration_s: float


# Изоляция каждого вызова модели от окружения машины. Проверено 2026-09-25
# (план итерации 3, шаг 0): без этих флагов агенту сервиса были доступны
# 75 инструментов — Bash, Write, Edit и 49 инструментов Gmail / Google Drive /
# Claude Docs, Bash выполнялся без запроса разрешения (~/.claude/settings.local.json
# спарка разрешает его везде; --allowedTools набор не ограничивает, только
# одобряет), в каждый вызов подмешивался личный ~/.claude/CLAUDE.md.
#   --strict-mcp-config       без --mcp-config — ни одного MCP-сервера;
#   --safe-mode               без CLAUDE.md, навыков, плагинов, хуков; настройки
#                             машины (переменные прокси) и вход по подписке остаются;
#   --permission-mode dontAsk всё, что не одобрено заранее, запрещается, а не ждёт ответа.
# Набор инструментов задаёт --tools в _build_cmd — ровно allowed_tools узла.
# --bare и --setting-sources не подходят: первый не читает подписку, второй
# отключает настройки с переменными прокси, без которых модель недоступна.
_ISOLATION_FLAGS = ["--strict-mcp-config", "--safe-mode", "--permission-mode", "dontAsk"]


def _build_cmd(
    message: str,
    schema_json: str | None,
    allowed_tools: list[str],
    resume_session_id: str | None,
    max_budget_usd: float | None,
    model: str = MODEL,
) -> list[str]:
    """
    Собрать argv для вызова `claude -p` (список аргументов, не строка через shell).

    schema_json=None — режим без --json-schema (текстовый ответ, call_text()).
    """
    cmd = ["claude", "-p"]
    if resume_session_id is not None:
        cmd += ["--resume", resume_session_id]
    cmd.append(message)
    if schema_json is not None:
        cmd += ["--json-schema", schema_json]
    cmd += [
        "--output-format", "json",
        "--tools", ",".join(allowed_tools),         # доступны ровно инструменты узла
        "--allowedTools", " ".join(allowed_tools),  # и они же одобрены без запроса
        *_ISOLATION_FLAGS,
        "--model", model,
        "--effort", EFFORT,
    ]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    return cmd


def _retry_message(error_text: str) -> str:
    """Текст ретрая через --resume: ошибка целиком + требование вернуть исправленный JSON."""
    return (
        "Предыдущий ответ не прошёл проверку. Текст ошибки:\n"
        f"{error_text}\n\n"
        "Верни исправленный JSON строго по заданной схеме, без пояснений вокруг."
    )


def _budget_exhausted(cost_total: float, max_budget_usd: float | None) -> bool:
    """Проверка лимита бюджета: дальше не ретраим, если он задан и уже исчерпан."""
    return max_budget_usd is not None and cost_total >= max_budget_usd


def _log_attempt(
    logger: logging.Logger,
    trace_name: str,
    attempt: int,
    max_attempts: int,
    duration_s: float,
    cost_usd: float,
    num_turns: int | None,
    outcome: str,
) -> None:
    """Единая строка лога попытки: артефакт, номер, длительность, стоимость, ходы, исход."""
    turns = str(num_turns) if num_turns is not None else "-"
    logger.info(
        "call: artifact=%s attempt=%d/%d duration=%.1fs cost=$%.4f turns=%s outcome=%s",
        trace_name, attempt, max_attempts, duration_s, cost_usd, turns, outcome,
    )


def _run_attempt(
    cmd: list[str],
    document: str | None,
    send_document: bool,
    timeout_s: int,
    run_id: str,
    trace_name: str,
) -> _AttemptOutcome:
    """
    Выполнить один запуск процесса claude -p: собрать kwargs, запустить, сохранить
    сырой ответ (ВСЕГДА, включая упавшие попытки), разобрать JSON верхнего уровня,
    найти событие result и проверить is_error.

    Общая часть для call() и call_text() — оба видят сбои процесса/API одинаково
    (полный повтор без --resume); что делать с содержимым самого result_text —
    решает вызывающая функция.
    """
    attempt_started = time.monotonic()
    run_kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout_s}
    if send_document and document is not None:
        run_kwargs["input"] = document
    else:
        run_kwargs["stdin"] = subprocess.DEVNULL

    try:
        proc = subprocess.run(cmd, **run_kwargs)
    except FileNotFoundError as exc:
        raise RuntimeError(f"бинарь 'claude' не найден: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        save_raw(run_id, trace_name, exc.stdout or "")
        return _AttemptOutcome(
            kind="timeout",
            error=f"таймаут процесса после {timeout_s} с",
            result_text=None, session_id=None, cost_usd=0.0, num_turns=None,
            permission_denials=[], duration_s=time.monotonic() - attempt_started,
        )

    duration = time.monotonic() - attempt_started
    raw_stdout = proc.stdout or ""
    save_raw(run_id, trace_name, raw_stdout)

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[:2000]
        return _AttemptOutcome(
            kind="process_error",
            error=f"процесс завершился с кодом {proc.returncode}: {stderr_tail}",
            result_text=None, session_id=None, cost_usd=0.0, num_turns=None,
            permission_denials=[], duration_s=duration,
        )

    try:
        events = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        return _AttemptOutcome(
            kind="top_level_json_error",
            error=f"вывод claude -p не разбирается как JSON: {exc}",
            result_text=None, session_id=None, cost_usd=0.0, num_turns=None,
            permission_denials=[], duration_s=duration,
        )

    result_event: dict[str, Any] | None = None
    if isinstance(events, list):
        result_event = next(
            (e for e in events if isinstance(e, dict) and e.get("type") == "result"), None
        )

    if result_event is None:
        return _AttemptOutcome(
            kind="no_result_event",
            error="в выводе claude -p нет события type == 'result'",
            result_text=None, session_id=None, cost_usd=0.0, num_turns=None,
            permission_denials=[], duration_s=duration,
        )

    session_id = result_event.get("session_id")
    cost_usd = float(result_event.get("total_cost_usd") or 0.0)
    num_turns = result_event.get("num_turns")
    permission_denials = result_event.get("permission_denials") or []
    result_text = result_event.get("result")

    if result_event.get("is_error"):
        return _AttemptOutcome(
            kind="api_error",
            error=f"ошибка API: {result_text}",
            result_text=None, session_id=session_id, cost_usd=cost_usd, num_turns=num_turns,
            permission_denials=permission_denials, duration_s=duration,
        )

    return _AttemptOutcome(
        kind="ok",
        error=None,
        result_text=result_text, session_id=session_id, cost_usd=cost_usd, num_turns=num_turns,
        permission_denials=permission_denials, duration_s=duration,
    )


def call(
    instruction: str,
    document: str | None,
    model_cls: type[ModelT],
    *,
    run_id: str,
    trace_name: str,
    allowed_tools: list[str],
    max_attempts: int = 3,
    timeout_s: int = 900,
    max_budget_usd: float | None = None,
    model: str = MODEL,
) -> CallResult[ModelT]:
    """
    Вызвать `claude -p` с ретраями и вернуть провалидированный ответ.

    document уходит в stdin ТОЛЬКО на попытках без --resume (первая попытка и
    ретраи после сбоя процесса/API) — при ретрае через --resume модель уже
    помнит документ из истории диалога, повторно слать его не нужно.

    max_budget_usd, если задан, действует на двух уровнях: передаётся в саму
    CLI как --max-budget-usd (потолок на один вызов процесса) и проверяется в
    Python как сумма стоимости по всем попыткам (потолок на весь call()).

    Исключение бросается только если ретраем чинить бессмысленно: бинарь
    claude не найден, либо сама функция вызвана с неверными аргументами.
    Исчерпание попыток — НЕ исключение, а CallResult(ok=False, ...); решение,
    что делать дальше, принимает вызывающий узел.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts должен быть >= 1")

    logger = get_run_logger(run_id)
    schema_json = json.dumps(model_cls.model_json_schema(), ensure_ascii=False)

    started = time.monotonic()
    cost_total = 0.0
    session_id: str | None = None
    last_error: str | None = None
    value: ModelT | None = None
    attempts_made = 0
    permission_denials_total: list[Any] = []

    # Состояние следующей попытки: что слать и слать ли документ в stdin
    message = instruction
    resume_session_id: str | None = None
    send_document = True

    for attempt in range(1, max_attempts + 1):
        attempts_made = attempt

        cmd = _build_cmd(message, schema_json, allowed_tools, resume_session_id, max_budget_usd, model)
        outcome = _run_attempt(cmd, document, send_document, timeout_s, run_id, trace_name)

        session_id = outcome.session_id or session_id
        cost_total += outcome.cost_usd
        permission_denials_total.extend(outcome.permission_denials)
        if outcome.permission_denials:
            logger.warning(
                "call: artifact=%s attempt=%d permission_denials=%s",
                trace_name, attempt, outcome.permission_denials,
            )

        if outcome.kind != "ok":
            # Сбой процесса/API (таймаут, ненулевой код, битый JSON верхнего уровня,
            # нет события result, is_error) -> полный повтор без --resume
            last_error = outcome.error
            _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                         outcome.cost_usd, outcome.num_turns, outcome.kind)
            resume_session_id, message, send_document = None, instruction, True
            if _budget_exhausted(cost_total, max_budget_usd):
                break
            continue

        try:
            parsed = json.loads(outcome.result_text)
        except (json.JSONDecodeError, TypeError) as exc:
            last_error = f"поле result не разбирается как JSON: {exc}"
            _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                         outcome.cost_usd, outcome.num_turns, "result_json_error")
            if outcome.session_id is not None:
                resume_session_id, message, send_document = (
                    outcome.session_id, _retry_message(last_error), False
                )
            else:
                # Защита: без session_id resume невозможен — начинаем заново
                resume_session_id, message, send_document = None, instruction, True
            if _budget_exhausted(cost_total, max_budget_usd):
                break
            continue

        try:
            value = model_cls.model_validate(parsed)
        except ValidationError as exc:
            last_error = str(exc)
            _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                         outcome.cost_usd, outcome.num_turns, "validation_error")
            if outcome.session_id is not None:
                resume_session_id, message, send_document = (
                    outcome.session_id, _retry_message(last_error), False
                )
            else:
                resume_session_id, message, send_document = None, instruction, True
            if _budget_exhausted(cost_total, max_budget_usd):
                break
            continue

        # Успех
        last_error = None
        _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                     outcome.cost_usd, outcome.num_turns, "ok")
        break

    duration_total = time.monotonic() - started
    return CallResult(
        value=value,
        ok=value is not None,
        attempts=attempts_made,
        cost_usd=cost_total,
        session_id=session_id,
        error=last_error,
        permission_denials=permission_denials_total,
        duration_s=duration_total,
    )


def call_text(
    instruction: str,
    document: str | None,
    *,
    run_id: str,
    trace_name: str,
    allowed_tools: list[str],
    max_attempts: int = 3,
    timeout_s: int = 900,
    max_budget_usd: float | None = None,
) -> CallResult[str]:
    """
    Вызвать `claude -p` в текстовом режиме (без --json-schema) и вернуть текст ответа.

    Для узлов, которым нужен свободный markdown/текст, а не JSON по pydantic-модели
    (например саммари приказа). document уходит в stdin на КАЖДОЙ попытке — --resume
    в этом режиме не используется вообще (см. модуль docstring: содержательной
    проверки ответа нет, поэтому чинить через диалог нечего — только полный повтор).

    Ретраятся только сбои процесса/API (таймаут, ненулевой код возврата, невалидный
    JSON верхнего уровня, отсутствие события result, is_error: true) и пустой/
    пробельный ответ модели — все полным повтором.

    Исчерпание попыток — НЕ исключение, а CallResult(ok=False, ...), как и в call().
    """
    if max_attempts < 1:
        raise ValueError("max_attempts должен быть >= 1")

    logger = get_run_logger(run_id)

    started = time.monotonic()
    cost_total = 0.0
    session_id: str | None = None
    last_error: str | None = None
    value: str | None = None
    attempts_made = 0
    permission_denials_total: list[Any] = []

    for attempt in range(1, max_attempts + 1):
        attempts_made = attempt

        cmd = _build_cmd(instruction, None, allowed_tools, None, max_budget_usd)
        outcome = _run_attempt(cmd, document, True, timeout_s, run_id, trace_name)

        session_id = outcome.session_id or session_id
        cost_total += outcome.cost_usd
        permission_denials_total.extend(outcome.permission_denials)
        if outcome.permission_denials:
            logger.warning(
                "call_text: artifact=%s attempt=%d permission_denials=%s",
                trace_name, attempt, outcome.permission_denials,
            )

        if outcome.kind != "ok":
            last_error = outcome.error
            _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                         outcome.cost_usd, outcome.num_turns, outcome.kind)
            if _budget_exhausted(cost_total, max_budget_usd):
                break
            continue

        if outcome.result_text is None or not outcome.result_text.strip():
            last_error = "пустой ответ модели"
            _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                         outcome.cost_usd, outcome.num_turns, "empty_response")
            if _budget_exhausted(cost_total, max_budget_usd):
                break
            continue

        # Успех
        value = outcome.result_text
        last_error = None
        _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                     outcome.cost_usd, outcome.num_turns, "ok")
        break

    duration_total = time.monotonic() - started
    return CallResult(
        value=value,
        ok=value is not None,
        attempts=attempts_made,
        cost_usd=cost_total,
        session_id=session_id,
        error=last_error,
        permission_denials=permission_denials_total,
        duration_s=duration_total,
    )
