"""
Общий вызов модели П-10: Claude CLI и Codex CLI.

30.10: P10_LLM_BACKEND выбирает движок; проверки контрактов и повторы общие.
Codex использует gpt-6.1-terra/medium, схему файлом, stdin и сохранённую авторизацию.
Codex получает только разрешённые узлу поиск и MCP-инструменты Гаранта/чтения прогона.
Чаты сохраняют происхождение сессий. Денежная стоимость Codex неизвестна; токены учитываются
по разнице накопленных значений сессии. Ниже — подробности прежнего режима Claude.

Берёт на себя: сборку команды, передачу документа через stdin, разбор вывода
`--output-format stream-json`, валидацию ответа и ретраи при сбоях. Про доменную логику
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

Ход агента построчно (2.7, мониторинг). CLI запускается с `--output-format stream-json
--verbose`: события (поиск, вызов Гаранта, ответ инструмента, итог) приходят по одному,
пока агент работает, и каждое сразу пишется в файл хода рядом с сырым ответом
(03_verdicts/03.stream.jsonl: время, номер попытки, событие). При обрыве по таймауту
в нём остаётся всё, что агент успел. Проба 13.10 (CLI 2.1.292): события stream-json те же,
что список при `--output-format json`, поэтому сырой ответ .raw.json собирается из них
и остаётся прежнего вида — его читают диалог, трейсы и скрипты сравнения (см. _exec).

Кэш (2.6). Общий для группы агентов текст (акт и паспорт) передаётся не в stdin,
а файлом в системный промпт (--append-system-prompt-file, shared_context): кэш
модели берёт куски запроса целиком, и только так агенты с разными заданиями читают
акт из кэша друг у друга, а не пишут его каждый заново. Файл лежит в
runs/<id>/_context/ — часть трейса. Срок кэша для пайплайна — 5 минут
(FORCE_PROMPT_CACHING_5M=1, запись 1,25× цены входа вместо 2× у часового); диалог
оставляет срок по умолчанию (1 час) — человек думает между сообщениями.

Бюджет — два независимых уровня защиты: max_budget_usd передаётся в саму CLI
как --max-budget-usd (потолок на ОДИН вызов процесса — останавливает
зациклившийся вызов до того, как он потратит много) и отдельно проверяется в
Python как сумма total_cost_usd по всем попыткам (потолок на весь call()/call_text()).

ГАРАНТ (2.7, итерация 4, доработка Б). Узел передаёт mcp_tools — имена инструментов
MCP-сервера Гаранта, которые разрешены агенту (без префикса mcp__garant__). Тогда
агент запускается с сервером Гаранта; видит он все его инструменты, но вызвать может
только перечисленные — остальные запрещены (--permission-mode dontAsk). Проба
2026-10-12 (CLI 2.1.292): --safe-mode отключает MCP-серверы целиком, поэтому для
такого агента изоляция собирается иначе — см. _MCP_ISOLATION_FLAGS. Токен Гаранта
и прокси машины уходят только в окружение процесса этого вызова. Неверный или
просроченный токен CLI не останавливает: инструмент вернёт агенту «ошибка
аутентификации», и это будет видно в его разборе и в сыром ответе.

permission_denials собираются по ВСЕМ попыткам в CallResult.permission_denials —
непустой список означает, что промпту узла зачем-то понадобился инструмент,
на который не было прав (сигнал кривого промпта, особенно у текстовых
индикаторов). Кроме попадания в CallResult, это же самое пишется в лог
отдельной предупреждающей строкой при получении события.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading

from app.formatting import format_cost
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, replace
import tempfile
import copy
import uuid
import sys
from app import config
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError, Field

from app.config import (
    CLI_SETTINGS_FILE, EFFORT, GARANT_ALL_TOOLS, GARANT_MAX_OUTPUT_TOKENS, GARANT_MCP_CONFIG, GARANT_NO_PROXY_HOST,
    GARANT_TOKEN_FILE, MODEL, SERVICE_SYSTEM_PROMPT,
)
from app.json_fix import repair_json
from app.logger import get_run_logger
from app.trace import VERDICTS_DIR, log_event, run_dir, save_bytes, save_raw, record_codex_usage

# Тип модели ответа для call() — только pydantic-модели (--json-schema требует схему)
ModelT = TypeVar("ModelT", bound=BaseModel)

# Тип значения в CallResult — шире, чем ModelT: call() кладёт pydantic-модель,
# call_text() кладёт обычную строку. Без bound=BaseModel, иначе CallResult[str]
# был бы некорректен по типам.
ValueT = TypeVar("ValueT")


@dataclass
class CallResult(Generic[ValueT]):
    """Итог любого CLI; cost_usd полон только при cost_known, usage_complete отделяет неполные токены."""

    value: ValueT | None
    ok: bool
    attempts: int
    cost_usd: float
    session_id: str | None
    error: str | None
    permission_denials: list[Any]
    duration_s: float
    # Стоимость всей сессии CLI, как её сообщил последний запуск (14.10: при --resume CLI сообщает стоимость
    # сессии с начала, а не одного запуска) — помощник хранит её, чтобы следующий ход считал только свой расход
    session_cost_usd: float = 0.0
    backend: str = "claude"
    model: str | None = None
    cost_known: bool = True
    token_usage: dict[str, int] | None = None
    session_token_usage: dict[str, int] | None = None
    usage_complete: bool = True


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

    first_answer — первый ответ модели в StructuredOutput (см. _first_structured_answer).
    """

    kind: str
    error: str | None
    result_text: str | None
    session_id: str | None
    cost_usd: float
    num_turns: int | None
    permission_denials: list[Any]
    duration_s: float
    first_answer: str | None = None
    session_token_usage: dict[str, int] | None = None


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
# отключает настройки с переменными прокси, без которых модель недоступна
# (агенту с Гарантом прокси передаются окружением — см. _MCP_ISOLATION_FLAGS).
_ISOLATION_FLAGS = ["--strict-mcp-config", "--safe-mode", "--permission-mode", "dontAsk"]

# Изоляция агента с Гарантом (итерация 4, Б). Пробы 2026-10-12 на спарке (CLI 2.1.292):
#   - с --safe-mode MCP-сервер не подключается (mcp_servers: []);
#   - --setting-sources "" — ни один файл настроек не читается: ни настройки машины
#     (личный CLAUDE.md, плагины, хуки), ни .claude/settings.local.json рабочей папки
#     (с «local» он читался — проверено подменой языка ответа);
#   - --disable-slash-commands — без навыков;
#   - автопамять CLI по проекту отключает окружение (_mcp_env): без этого она была
#     включена (memory_paths у init-события), с ним — нет.
# Проверено: сервер Гаранта connected, CLAUDE.md и память в контексте нет. Прокси из
# настроек машины при этом тоже не читаются — их передаёт окружение процесса.
#   --strict-mcp-config + --mcp-config — только сервер Гаранта из файла репо;
#   --permission-mode dontAsk — вызвать можно только перечисленное в --allowedTools.
_MCP_ISOLATION_FLAGS = [
    "--strict-mcp-config", "--mcp-config", str(GARANT_MCP_CONFIG),
    "--setting-sources", "", "--disable-slash-commands",
    "--permission-mode", "dontAsk",
]
# Префикс имён инструментов сервера Гаранта в CLI: mcp__<имя сервера>__<инструмент>
_GARANT_TOOL_PREFIX = "mcp__garant__"
# Переменные из настроек CLI машины, которые нужны агенту с Гарантом: без прокси модель недоступна
_PROXY_ENV_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
# Автопамять CLI по проекту — не для агентов сервиса (без --safe-mode она включена)
_NO_AUTO_MEMORY_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def _build_cmd(
    message: str,
    schema_json: str | None,
    allowed_tools: list[str],
    resume_session_id: str | None,
    max_budget_usd: float | None,
    model: str = MODEL,
    system_file: str | None = None,
    mcp_tools: list[str] | None = None,
    mcp_config: str | None = None,
) -> list[str]:
    """
    Собрать argv для вызова `claude -p` (список аргументов, не строка через shell).

    schema_json=None — режим без --json-schema (текстовый ответ, call_text()).
    mcp_tools — разрешённые инструменты MCP: короткое имя — инструмент Гаранта, полное «mcp__…» — инструмент
    другого сервера из mcp_config (помощник, 14.10); пусто/None — вызов без MCP, как раньше.
    mcp_config — свой файл серверов MCP вместо файла Гаранта (помощник: Гарант + чтение артефактов прогона).
    """
    cmd = ["claude", "-p"]
    if resume_session_id is not None:
        cmd += ["--resume", resume_session_id]
    cmd.append(message)
    if schema_json is not None:
        cmd += ["--json-schema", schema_json]
    # Инструменты Гаранта одобряются по полным именам; встроенные (--tools) — как раньше
    approved = allowed_tools + [_mcp_tool_name(t) for t in (mcp_tools or [])]
    if schema_json is not None:
        cmd.append("--include-partial-messages")   # первый ответ модели целиком — см. _first_structured_answer
    cmd += [
        "--output-format", "stream-json", "--verbose",
        "--tools", ",".join(allowed_tools),         # доступны ровно встроенные инструменты узла
        "--allowedTools", " ".join(approved),       # и они же (плюс Гарант) одобрены без запроса
        *(_mcp_isolation_flags(mcp_config) if mcp_tools else _ISOLATION_FLAGS),
        "--model", model,
        "--effort", EFFORT,
    ]
    if mcp_tools:
        # 14.10: неразрешённые инструменты Гаранта — из списка модели долой (без этого видны все 18)
        denied = [_GARANT_TOOL_PREFIX + t for t in GARANT_ALL_TOOLS if _GARANT_TOOL_PREFIX + t not in approved]
        if denied:
            cmd += ["--disallowedTools", " ".join(denied)]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    # 14.10: свой системный промпт ВМЕСТО стандартного Claude Code; общий контекст группы агентов — в нём же (кэш)
    if system_file is not None:
        cmd += ["--system-prompt-file", system_file]
    else:
        cmd += ["--system-prompt", SERVICE_SYSTEM_PROMPT]
    return cmd


def _mcp_tool_name(tool: str) -> str:
    """Полное имя инструмента MCP для CLI: короткое — инструмент Гаранта, «mcp__…» — уже полное."""
    return tool if tool.startswith("mcp__") else _GARANT_TOOL_PREFIX + tool


def _mcp_isolation_flags(mcp_config: str | None) -> list[str]:
    """Флаги изоляции агента с MCP; mcp_config — свой файл серверов вместо файла Гаранта (помощник)."""
    if mcp_config is None:
        return _MCP_ISOLATION_FLAGS
    flags = list(_MCP_ISOLATION_FLAGS)
    flags[flags.index("--mcp-config") + 1] = mcp_config
    return flags


# Срок кэша 5 минут для вызовов пайплайна: чтения внутри прогона идут через секунды и минуты
_SHORT_CACHE_ENV = {"FORCE_PROMPT_CACHING_5M": "1"}

# Запасной ответ (принятый CLI после переписывания) вместо битого JSON первого ответа берётся, только если он
# не короче этой доли первого: иначе он урезан. Замер 15.10 на 40 случаях: 0,70–1,0; паспорт 13.10 — 0,49.
MIN_FALLBACK_SHARE = 0.6

# Имя инструмента, через который CLI принимает ответ по --json-schema
_STRUCTURED_TOOL = "StructuredOutput"


def _first_structured_answer(events: list[Any]) -> str | None:
    """
    Полный текст ПЕРВОГО ответа модели в StructuredOutput — из частичных сообщений
    (--include-partial-messages). Битый JSON CLI отвергает, модель переписывает ответ
    и урезает содержимое (паспорт приютов: 16,6 тыс. символов → 8,1 тыс.), поэтому берётся
    первый ответ, а битый — чинится по схеме (app/json_fix.py). Не починился или не прошёл контракт — call()
    берёт ответ, принятый CLI, если он не урезан (15.10, MIN_FALLBACK_SHARE). None — частичных сообщений нет.
    """
    current: int | None = None
    parts: list[str] = []
    for event in events:
        if not (isinstance(event, dict) and event.get("type") == "stream_event"):
            continue
        e = event.get("event") or {}
        if e.get("type") == "content_block_start":
            block = e.get("content_block") or {}
            if current is None and block.get("type") == "tool_use" and block.get("name") == _STRUCTURED_TOOL:
                current = e.get("index")
        elif current is not None and e.get("index") == current:
            delta = e.get("delta") or {}
            if e.get("type") == "content_block_delta" and delta.get("type") == "input_json_delta":
                parts.append(delta.get("partial_json", ""))
            elif e.get("type") == "content_block_stop":
                return "".join(parts)
    return None


def _shared_context_file(run_id: str, shared_context: str) -> str:
    """Файл системного промпта в папке прогона: наш короткий системный промпт и общий контекст группы агентов —
    один файл на содержимое (одинаковый текст — один файл)."""
    data = f"{SERVICE_SYSTEM_PROMPT}\n\n{shared_context}".encode("utf-8")
    relname = f"_context/{hashlib.sha1(data).hexdigest()[:12]}.md"
    return str(save_bytes(run_id, relname, data))


def _garant_token() -> str:
    """Токен Гаранта из файла машины; нет файла, пуст или испорчен — RuntimeError с понятной причиной."""
    try:
        token = GARANT_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"нет файла с токеном Гаранта или он не читается ({GARANT_TOKEN_FILE})") from exc
    if not token or any(ch.isspace() for ch in token):
        raise RuntimeError(f"файл с токеном Гаранта пуст или испорчен ({GARANT_TOKEN_FILE})")
    return token


def garant_available() -> bool:
    """
    Можно ли дать агенту Гарант: файл токена есть и он исправен (15.10, код-ревью). Где Гарант — подсказка
    (паспорт, помощник), без токена агент работает без него; индикаторам Гарант нужен — там call() падает с
    понятной причиной, как раньше.
    """
    try:
        _garant_token()
    except RuntimeError:
        return False
    return True


def _mcp_env(with_garant: bool = True) -> dict[str, str]:
    """
    Окружение процесса агента с инструментами MCP: прокси из настроек CLI и, если у агента есть инструменты
    Гаранта, токен из файла машины (15.10: свои инструменты — чтение артефактов прогона — токена не требуют).
    Токен никуда не пишется — ни в команду, ни в лог, ни в трейс.
    """
    env = {**_NO_AUTO_MEMORY_ENV}
    if with_garant:
        env["GARANT_TOKEN"] = _garant_token()
    # Прокси: файл настроек может отсутствовать (на машине без прокси это нормально) —
    # тогда переменных нет; испорченный файл не роняет вызов, но и прокси не даёт
    try:
        cli_env = json.loads(CLI_SETTINGS_FILE.read_text(encoding="utf-8")).get("env") or {}
        env.update({k: str(cli_env[k]) for k in _PROXY_ENV_KEYS if k in cli_env})
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    # Гарант — напрямую: через прокси вызовы зависают и рвутся (см. GARANT_NO_PROXY_HOST)
    env["NO_PROXY"] = ",".join(h for h in (env.get("NO_PROXY", ""), GARANT_NO_PROXY_HOST) if h)
    # Большой ответ Гаранта — агенту целиком, а не в файл, который ему нечем прочитать
    env["MAX_MCP_OUTPUT_TOKENS"] = str(GARANT_MAX_OUTPUT_TOKENS)
    return env


def _is_json(text: str) -> bool:
    """Разбирается ли текст как JSON без починки."""
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def _parse_answer(answer: str | None, schema: dict[str, Any]) -> Any:
    """Ответ модели → объект: как есть, а если JSON битый — после починки по схеме контракта."""
    try:
        return json.loads(answer)
    except json.JSONDecodeError:
        return json.loads(repair_json(answer, schema))


def _accept_answer(answer: str | None, schema: dict[str, Any], model_cls: type[ModelT],
                   validation_context: dict[str, Any] | None) -> tuple[ModelT | None, str, str]:
    """
    Один ответ модели → (значение контракта, вид сбоя, текст ошибки). Значение None — ответ не подошёл:
    «result_json_error» — не разбирается и не чинится, «validation_error» — не проходит контракт.
    """
    try:
        parsed = _parse_answer(answer, schema)
    except (json.JSONDecodeError, TypeError) as exc:
        return None, "result_json_error", f"поле result не разбирается как JSON: {exc}"
    try:
        return model_cls.model_validate(parsed, context=validation_context), "ok", ""
    except ValidationError as exc:
        return None, "validation_error", str(exc)


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
    *,
    run_id: str,
    error: str | None,
) -> None:
    """Единая строка лога попытки: артефакт, номер, длительность, стоимость, ходы, исход — и событие журнала сессии."""
    turns = str(num_turns) if num_turns is not None else "-"
    logger.info(
        "call: artifact=%s attempt=%d/%d duration=%.1fs cost=%s turns=%s outcome=%s",
        trace_name, attempt, max_attempts, duration_s,
        format_cost(cost_usd, config.LLM_BACKEND == "claude", digits=4), turns, outcome,
    )
    log_event(run_id, "agent_attempt", who=_agent_label(trace_name), trace=trace_name, attempt=attempt,
              max_attempts=max_attempts, outcome=outcome, duration_s=round(duration_s, 1), cost_usd=cost_usd,
              turns=num_turns, error=(error or "")[:500] or None,
              backend=config.LLM_BACKEND, cost_known=config.LLM_BACKEND == "claude")


def _agent_label(trace_name: str) -> str:
    """Кто в журнале сессии: «агент <артефакт без .raw.json>» — по нему видно узел и номер индикатора."""
    return "агент " + trace_name.removesuffix(".raw.json")


def _log_agent_started(run_id: str, trace_name: str, model: str, allowed_tools: list[str],
                       mcp_tools: list[str] | None, timeout_s: int) -> None:
    """Событие журнала: агент запущен — модель, встроенные инструменты, инструменты Гаранта, таймаут."""
    log_event(run_id, "agent_started", who=_agent_label(trace_name), trace=trace_name, model=model,
              tools=list(allowed_tools), garant=list(mcp_tools or []), timeout_s=timeout_s, backend=config.LLM_BACKEND)


def _log_agent_finished(run_id: str, trace_name: str, result: "CallResult") -> None:
    """Событие журнала: итог вызова агента — успех, число попыток, стоимость, время, последняя ошибка."""
    log_event(run_id, "agent_finished", who=_agent_label(trace_name), trace=trace_name, ok=result.ok,
              attempts=result.attempts, cost_usd=result.cost_usd, duration_s=round(result.duration_s, 1),
              error=(result.error or "")[:500] or None, backend=result.backend, model=result.model,
              cost_known=result.cost_known, token_usage=result.token_usage, usage_complete=result.usage_complete)


def _stream_path(run_id: str, trace_name: str) -> Path:
    """Файл хода агента рядом с его сырым ответом: 03_verdicts/03.raw.json → 03_verdicts/03.stream.jsonl."""
    return run_dir(run_id) / (trace_name.removesuffix(".raw.json") + ".stream.jsonl")


def _as_stdout(lines: list[str]) -> str:
    """
    Строки stream-json → stdout прежнего вида (до 2.7 — `--output-format json`): JSON-список
    тех же событий. Если хоть одна строка не JSON — текст как есть: цикл попыток увидит
    «вывод не разбирается как JSON», как и раньше.
    """
    events = []
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            return "".join(lines)
    return json.dumps(events, ensure_ascii=False)


def _exec(
    cmd: list[str],
    *,
    timeout_s: int,
    env: dict[str, str] | None,
    document: str | None,
    stream_path: Path,
    attempt: int,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    """
    Запустить claude -p и читать его вывод построчно: каждое событие сразу дописывается в файл
    хода (время, попытка, событие — или text, если строка не JSON). Документ уходит в stdin
    отдельным потоком, stderr читается своим — иначе большой документ и полный канал вывода
    заперли бы друг друга.

    Возвращает CompletedProcess со stdout прежнего вида (см. _as_stdout). Таймаут — процесс
    убивается, бросается subprocess.TimeoutExpired с тем, что агент успел вывести (output),
    как это делал subprocess.run.
    """
    cwd_options = {"cwd": cwd} if cwd is not None else {}
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE if document is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env, **cwd_options,
    )
    lines: list[str] = []
    stderr_parts: list[str] = []
    stream_path.parent.mkdir(parents=True, exist_ok=True)

    def feed() -> None:
        # Документ в stdin; агент может закрыть вход раньше — это не сбой записи
        try:
            proc.stdin.write(document)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def read_out() -> None:
        # Каждая строка — в файл хода сразу (flush), чтобы её было видно до конца работы агента
        with open(stream_path, "a", encoding="utf-8") as f:
            for line in proc.stdout:
                lines.append(line)
                if not line.strip():
                    continue
                entry: dict[str, Any] = {"ts": datetime.now().isoformat(timespec="milliseconds"), "attempt": attempt}
                try:
                    entry["event"] = json.loads(line)
                except json.JSONDecodeError:
                    entry["text"] = line.rstrip("\n")
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()

    def read_err() -> None:
        stderr_parts.append(proc.stderr.read())

    threads = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    if document is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for t in threads:
            t.join(timeout=10)
        raise subprocess.TimeoutExpired(cmd, timeout_s, output=_as_stdout(lines), stderr="".join(stderr_parts)) from None
    for t in threads:
        t.join()
    return subprocess.CompletedProcess(cmd, proc.returncode, _as_stdout(lines), "".join(stderr_parts))


def _run_attempt(
    cmd: list[str],
    document: str | None,
    send_document: bool,
    timeout_s: int,
    run_id: str,
    trace_name: str,
    env: dict[str, str] | None = None,
    attempt: int = 1,
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
    try:
        proc = _exec(cmd, timeout_s=timeout_s, env=env, document=document if send_document else None,
                     stream_path=_stream_path(run_id, trace_name), attempt=attempt)
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
        first_answer=_first_structured_answer(events),
    )


def codex_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Строгая форма для CLI: значения и ограничения сохраняются, исходник не меняется."""
    def convert(node):
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        # properties/$defs — словари имён, а не схемы: поле с именем default нельзя удалить.
        result = {}
        for key, value in node.items():
            if key == "default":
                continue
            if key in ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas"):
                result[key] = {name: convert(child) for name, child in value.items()}
            elif key in ("enum", "const", "examples", "dependentRequired"):
                result[key] = copy.deepcopy(value)
            else:
                result[key] = convert(value)
        if isinstance(result.get("additionalProperties"), dict) or result.get("additionalProperties") is True:
            raise ValueError("Codex: свободный словарь в схеме требует отдельной проверки совместимости")
        if "properties" in result:
            result["additionalProperties"] = False
            result["required"] = list(result["properties"])
        return result
    return convert(schema)


_CODEX_COUNTERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                   "output_tokens", "reasoning_output_tokens")


def _codex_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict) or not {"input_tokens", "output_tokens"} <= value.keys():
        return None
    usage = {key: value[key] for key in _CODEX_COUNTERS if key in value}
    if any(type(amount) is not int or amount < 0 for amount in usage.values()):
        raise ValueError("usage_error: счётчики токенов должны быть неотрицательными целыми")
    return usage


def _usage_delta(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    """CLI сообщает накопленное по сессии — проба 30.10 подтверждена её журналом."""
    if not previous.keys() <= current.keys():
        raise ValueError("usage_error: в продолжении пропали прежние счётчики")
    delta = {key: amount - previous.get(key, 0) for key, amount in current.items()}
    if any(amount < 0 for amount in delta.values()):
        raise ValueError("usage_error: накопленный расход сессии уменьшился")
    return delta


def _valid_codex_session(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


class _CodexInvocation:
    """Техническая обёртка Codex. Проверки контракта и цикл повторов остаются в call()."""

    def __init__(self, run_id: str, trace_name: str, model: str,
                 schema: dict[str, Any], shared_context: str | None, allowed_tools: list[str],
                 mcp_tools: list[str] | None, mcp_config: str | None):
        self.run_id, self.trace_name, self.model = run_id, trace_name, model
        self.workspace = (run_dir(run_id) / "_codex" / "workspace").resolve()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = Path(tempfile.mkdtemp(prefix="call-", dir=self.workspace))
        self.schema_file = self.directory / "schema.json"
        self.system_file = self.directory / "instructions.md"
        for path, text in (
            (self.schema_file, json.dumps(codex_output_schema(schema), ensure_ascii=False)),
            (self.system_file, SERVICE_SYSTEM_PROMPT + ("\n\n" + shared_context if shared_context else "")),
        ):
            save_bytes(run_id, path.relative_to(run_dir(run_id).resolve()).as_posix(), text.encode())
        self.env = dict(os.environ)
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "GARANT_TOKEN", "FORCE_PROMPT_CACHING_5M"):
            self.env.pop(key, None)
        self.web_enabled = bool(allowed_tools)
        if set(allowed_tools) not in (set(), {"WebSearch", "WebFetch"}):
            raise ValueError("Codex: разрешена только пара WebSearch/WebFetch либо отсутствие поиска")
        self.mcp_options: dict[str, Any] = {}
        self.allowed_mcp: set[tuple[str, str]] = set()
        if mcp_tools:
            servers = json.loads(Path(mcp_config or GARANT_MCP_CONFIG).read_text(encoding="utf-8"))["mcpServers"]
            groups: dict[str, list[str]] = {}
            allowed_methods = {
                "garant": GARANT_ALL_TOOLS,
                "p10run": ("list_artifacts", "read_artifact"),
                "garantweb": ("garant_web",),
            }
            for tool in mcp_tools:
                name = _mcp_tool_name(tool)
                parts = name.split("__", 2)
                if len(parts) != 3 or parts[0] != "mcp":
                    raise ValueError("Codex: неверное имя инструмента MCP")
                server, method = parts[1:]
                if method not in allowed_methods.get(server, ()):
                    raise ValueError("Codex: неизвестный инструмент MCP")
                groups.setdefault(server, [])
                if method not in groups[server]:
                    groups[server].append(method)
                self.allowed_mcp.add((server, method))
            for server, methods in groups.items():
                source = servers.get(server)
                if not isinstance(source, dict):
                    raise ValueError("Codex: сервер MCP отсутствует в конфигурации")
                prefix = f"mcp_servers.{server}."
                if server == "garant":
                    if source.get("command"):
                        from app.mcp_garant import ALLOWED
                        cache = (source.get("env") or {}).get("P10_GARANT_CACHE_DIR", "")
                        root = (run_dir(run_id) / "_garant").resolve()
                        if (Path(source["command"]).resolve() != Path(sys.executable).resolve()
                                or source.get("args") != [str(config.PROJECT_ROOT / "app/mcp_garant.py"),
                                                         str(run_dir(run_id).resolve() / VERDICTS_DIR / "03.garant")]
                                or not cache or root not in Path(cache).resolve().parents
                                or source.get("env") != {"P10_GARANT_CACHE_DIR": cache}
                                or not set(methods).issubset(ALLOWED)):
                            raise ValueError("Codex: неверная изолированная обёртка Гаранта")
                        self.mcp_options[prefix + "command"] = source["command"]
                        self.mcp_options[prefix + "args"] = source["args"]
                        self.mcp_options[prefix + "env.P10_GARANT_CACHE_DIR"] = cache
                        self.mcp_options[prefix + "env_vars"] = ["GARANT_TOKEN"]
                    else:
                        if source.get("url") != "https://mcp.garant.ru/mcp" or source.get("headers", {}) not in ({}, {"Authorization": "Bearer ${GARANT_TOKEN}"}):
                            raise ValueError("Codex: Гарант требует штатный адрес и токен только из окружения")
                        self.mcp_options[prefix + "url"] = source["url"]
                        self.mcp_options[prefix + "bearer_token_env_var"] = "GARANT_TOKEN"
                    self.env.update(_mcp_env())
                elif server == "garantweb":
                    expected = {"type": "stdio", "command": sys.executable,
                                "args": [str(config.PROJECT_ROOT / "app/mcp_garant_web.py")],
                                "env": {"GARANT_TOKEN": ""}}
                    if source != expected:
                        raise ValueError("Codex: неверная конфигурация веб-чтения Гаранта")
                    self.mcp_options[prefix + "command"] = source["command"]
                    self.mcp_options[prefix + "args"] = source["args"]
                    self.mcp_options[prefix + "env.GARANT_TOKEN"] = ""
                    self.mcp_options[prefix + "tool_timeout_sec"] = 250  # сам инструмент ждёт не больше 240 с
                else:
                    expected = str(run_dir(run_id).resolve())
                    if (Path(source.get("command", "")).resolve() != Path(sys.executable).resolve()
                            or source.get("args") != [str(config.PROJECT_ROOT / "app" / "mcp_run.py")]
                            or source.get("env") != {"P10_RUN_DIR": expected}):
                        raise ValueError("Codex: p10run должен читать только текущий прогон штатным сервером")
                    for key in ("command", "args"):
                        self.mcp_options[prefix + key] = source[key]
                    self.mcp_options[prefix + "env.P10_RUN_DIR"] = expected
                self.mcp_options[prefix + "required"] = True
                self.mcp_options[prefix + "enabled_tools"] = methods
                for method in methods:
                    self.mcp_options[prefix + f"tools.{method}.approval_mode"] = "approve"
                    self.mcp_options[prefix + f"tools.{method}.output_token_limit"] = GARANT_MAX_OUTPUT_TOKENS
        elif mcp_config:
            raise ValueError("Codex: конфигурация MCP без разрешённых методов не используется")

    def command(self, attempt: int, resume: str | None) -> tuple[list[str], Path]:
        output = self.directory / f"answer-{attempt}.json"
        command = ["codex", "exec"] + (["resume"] if resume else [])
        command += ["--ignore-user-config", "--ignore-rules", "--strict-config", "--skip-git-repo-check",
                    "--model", self.model, "--json", "--output-schema", str(self.schema_file),
                    "--output-last-message", str(output)]
        options = {
            "model_reasoning_effort": EFFORT, "approval_policy": "never", "sandbox_mode": "read-only",
            "web_search": "live" if self.web_enabled else "disabled", "project_doc_max_bytes": 0,
            "model_instructions_file": str(self.system_file),
            "features.shell_tool": False, "features.shell_snapshot": False, "features.apps": False,
            "features.multi_agent": False, "features.hooks": False, "features.memories": False,
            "features.skill_search": False, "features.skip_host_skill_discovery": True,
            "memories.use_memories": False, "memories.generate_memories": False,
            **self.mcp_options,
        }
        for key, value in options.items():
            command += ["-c", key + "=" + json.dumps(value, ensure_ascii=False)]
        command += [resume, "-"] if resume else ["-"]
        relative = (self.directory / f"attempt-{attempt}.command.json").relative_to(run_dir(self.run_id).resolve())
        save_bytes(self.run_id, relative.as_posix(), json.dumps(command, ensure_ascii=False).encode())
        return command, output

    def attempt(self, message: str, document: str | None, send_document: bool,
                resume: str | None, attempt: int, timeout_s: int) -> _AttemptOutcome:
        command, output = self.command(attempt, resume)
        text = message + ("\n\n" + document if send_document and document is not None else "")
        relative = (self.directory / f"attempt-{attempt}.input.txt").relative_to(run_dir(self.run_id).resolve())
        save_bytes(self.run_id, relative.as_posix(), text.encode())
        started = time.monotonic()
        raw, stderr = "", ""
        thread, usage = None, None

        def outcome(kind, error=None, answer=None):
            return _AttemptOutcome(kind=kind, error=(f"{kind}: {error}" if error else None),
                result_text=answer, session_id=thread, cost_usd=0.0, num_turns=None,
                permission_denials=[], duration_s=time.monotonic() - started,
                session_token_usage=usage)
        try:
            process = _exec(command, timeout_s=timeout_s, env=self.env, document=text,
                            stream_path=_stream_path(self.run_id, self.trace_name), attempt=attempt,
                            cwd=self.workspace)
            raw, stderr = process.stdout or "", process.stderr or ""
        except FileNotFoundError as error:
            raise RuntimeError("бинарь 'codex' не найден") from error
        except subprocess.TimeoutExpired as error:
            raw, stderr = error.stdout or "", error.stderr or ""
            return outcome("timeout", f"таймаут процесса после {timeout_s} с")
        finally:
            save_raw(self.run_id, self.trace_name, raw)
            save_raw(self.run_id, self.trace_name + ".stderr.txt", stderr)
        try:
            events = json.loads(raw)
            if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
                raise ValueError("ожидался список событий")
        except (ValueError, TypeError) as error:
            if process.returncode:
                return outcome("process_error", f"код {process.returncode}: {stderr[:2000]}")
            return outcome("top_level_json_error", str(error))
        thread = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
        completed = next((e for e in reversed(events) if e.get("type") == "turn.completed"), None)
        if completed is not None:
            try:
                usage = _codex_usage(completed.get("usage"))
            except ValueError as error:
                return outcome("usage_error", str(error))
        if process.returncode:
            return outcome("process_error", f"код {process.returncode}: {stderr[:2000]}")
        if resume and thread != resume:
            thread, usage = None, None
            return outcome("session_mismatch", "CLI продолжил другую сессию")
        for event in events:
            if event.get("type") == "error":
                message = event.get("message")
                if completed is not None and isinstance(message, str) and re.match(r"^Reconnecting\.\.\.\s+\d+/\d+\b", message):
                    log_event(self.run_id, "cli_notice", backend="codex", trace=self.trace_name, message=message)
                    continue
                return outcome("api_error", json.dumps(event, ensure_ascii=False)[:2000])
            if event.get("type") == "turn.failed":
                return outcome("api_error", json.dumps(event, ensure_ascii=False)[:2000])
            if str(event.get("type", "")).startswith("item."):
                item = event.get("item") or {}
                if not isinstance(item, dict):
                    return outcome("api_error", "повреждено событие item")
                kind = item.get("type")
                if kind == "error":
                    message = str(item.get("message", ""))
                    notice = message.startswith("Under-development features enabled:") or (
                        completed is not None and (
                            message.startswith(f"Model metadata for `{self.model}` not found. Defaulting to fallback metadata;")
                            or message.startswith("Falling back from WebSockets to HTTPS transport.")
                        ))
                    if notice:
                        log_event(self.run_id, "cli_notice", backend="codex", trace=self.trace_name, message=message)
                    else:
                        return outcome("api_error", message[:2000])
                elif kind == "mcp_tool_call":
                    # CLI может сначала запросить перечень ресурсов у своих настроенных MCP.
                    # Это чтение каталога; произвольные методы и серверы по-прежнему запрещены.
                    arguments = item.get("arguments") or {}
                    discovery = (
                        bool(self.allowed_mcp)
                        and item.get("server") in {"codex", *(server for server, _ in self.allowed_mcp)}
                        and item.get("tool") in ("list_mcp_resources", "list_mcp_resource_templates")
                        and isinstance(arguments, dict) and set(arguments) <= {"server", "cursor"}
                        and (arguments.get("server") is None
                             or (isinstance(arguments["server"], str)
                                 and arguments["server"] in {server for server, _ in self.allowed_mcp}))
                    )
                    if not discovery and (item.get("server"), item.get("tool")) not in self.allowed_mcp:
                        return outcome("unexpected_tool", "метод MCP не разрешён этому агенту")
                elif kind == "web_search":
                    if not self.web_enabled:
                        return outcome("unexpected_tool", "поиск не разрешён этому агенту")
                elif kind not in ("agent_message", "reasoning"):
                    return outcome("unexpected_tool", str(kind))
        if not isinstance(thread, str) or not thread or completed is None:
            return outcome("no_result", "нет thread.started или turn.completed")
        if not _valid_codex_session(thread):
            thread, usage = None, None
            return outcome("invalid_session", "thread_id должен быть UUID, не флагом CLI")
        try:
            answer = output.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return outcome("no_result", "нет читаемого файла итогового ответа")
        if not answer.strip():
            return outcome("no_result", "пустой итоговый ответ")
        return outcome("ok", answer=answer)


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
    model: str | None = None,
    shared_context: str | None = None,
    short_cache_ttl: bool = True,
    mcp_tools: list[str] | None = None,
    validation_context: dict[str, Any] | None = None,
    resume_session_id: str | None = None,
    mcp_config: str | None = None,
    resume_cost_base: float = 0.0,
    resume_backend: str | None = None,
    resume_token_base: dict[str, int] | None = None,
) -> CallResult[ModelT]:
    """
    Вызвать `claude -p` с ретраями и вернуть провалидированный ответ.

    validation_context (14.10) — данные для проверок контракта, которым нужен вход узла (например, текст акта,
    чтобы сверить цитату); уходит в model_validate(context=...).

    document уходит в stdin ТОЛЬКО на попытках без --resume (первая попытка и
    ретраи после сбоя процесса/API) — при ретрае через --resume модель уже
    помнит документ из истории диалога, повторно слать его не нужно.

    max_budget_usd, если задан, действует на двух уровнях: передаётся в саму
    CLI как --max-budget-usd (потолок на один вызов процесса) и проверяется в
    Python как сумма стоимости по всем попыткам (потолок на весь call()).

    shared_context — общий для группы агентов текст: уходит файлом в системный промпт
    (см. «Кэш» в docstring модуля), а не в stdin. short_cache_ttl — срок кэша 5 минут
    (пайплайн); False — срок по умолчанию, 1 час (диалог).

    mcp_tools — инструменты Гаранта, разрешённые агенту (см. «ГАРАНТ» в docstring модуля); полные имена «mcp__…» —
    инструменты других серверов из mcp_config.

    resume_session_id (помощник, 14.10) — продолжить сессию CLI: первая попытка идёт через --resume, document
    не шлётся (модель его уже видела). Полный повтор после сбоя процесса — тоже с --resume этой сессии: начать
    заново значит потерять разговор. Без него — поведение прежнее.
    mcp_config — свой файл серверов MCP вместо файла Гаранта (помощник: Гарант + чтение артефактов прогона).

    Стоимость при --resume (14.10, проба помощника): CLI сообщает total_cost_usd всей сессии с её начала, а не
    одного запуска (modelUsage второго хода = сумма двух ходов). Расход попытки через --resume = сообщённое минус
    прежнее сообщённое по этой сессии; resume_cost_base — прежнее сообщённое для resume_session_id (помощник
    берёт его из CallResult.session_cost_usd прошлого хода).

    Исключение бросается только если ретраем чинить бессмысленно: бинарь
    claude не найден, функция вызвана с неверными аргументами, либо агенту нужен
    Гарант, а файла токена нет или он испорчен (RuntimeError до запуска модели:
    тихо работать без Гаранта нельзя; узел превращает исключение в вердикт «ошибка»).
    Исчерпание попыток — НЕ исключение, а CallResult(ok=False, ...); решение,
    что делать дальше, принимает вызывающий узел.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts должен быть >= 1")

    backend = config.LLM_BACKEND
    model = model or config.BACKEND_MODELS[backend]
    if resume_session_id and (resume_backend or "claude") != backend:
        raise ValueError("сессия другого движка: начните новую с нужной историей")
    if backend == "codex":
        if resume_session_id and not _valid_codex_session(resume_session_id):
            raise ValueError("Codex resume принимает только явный UUID сессии")
        if model != config.BACKEND_MODELS["codex"]:
            raise ValueError("Codex в P-10 использует только gpt-6.1-terra")
        if max_budget_usd is not None:
            raise ValueError("Codex CLI не поддерживает заданный денежный лимит")
        if resume_session_id and _codex_usage(resume_token_base) is None:
            raise ValueError("для продолжения Codex нужен прежний накопленный расход токенов")

    logger = get_run_logger(run_id)
    schema = model_cls.model_json_schema()
    schema_json = json.dumps(schema, ensure_ascii=False)

    started = time.monotonic()
    cost_total = 0.0
    session_id: str | None = None
    last_error: str | None = None
    value: ModelT | None = None
    attempts_made = 0
    permission_denials_total: list[Any] = []

    codex = _CodexInvocation(run_id, trace_name, model, schema, shared_context, allowed_tools, mcp_tools, mcp_config) if backend == "codex" else None
    system_file = _shared_context_file(run_id, shared_context) if backend == "claude" and shared_context is not None else None
    env = {**os.environ, **_SHORT_CACHE_ENV} if backend == "claude" and short_cache_ttl else None
    if mcp_tools:
        with_garant = any(_mcp_tool_name(tool).startswith(_GARANT_TOOL_PREFIX) for tool in mcp_tools)
        env = {**(env if env is not None else os.environ), **_mcp_env(with_garant)}
    token_total: dict[str, int] = {}
    usage_complete = True
    token_reported = {resume_session_id: dict(resume_token_base)} if resume_session_id and resume_token_base is not None else {}
    _log_agent_started(run_id, trace_name, model, allowed_tools, mcp_tools, timeout_s)

    # Состояние следующей попытки: что слать и слать ли документ в stdin.
    # base_resume — с чего начинает попытка «с начала»: новая сессия или продолжение разговора помощника
    base_resume = resume_session_id
    message = instruction
    send_document = base_resume is None
    # Сколько CLI уже сообщил по каждой сессии: при --resume он сообщает стоимость сессии с начала
    reported: dict[str, float] = {resume_session_id: resume_cost_base} if resume_session_id else {}

    for attempt in range(1, max_attempts + 1):
        attempts_made = attempt

        if codex is None:
            cmd = _build_cmd(message, schema_json, allowed_tools, resume_session_id, max_budget_usd, model, system_file,
                             mcp_tools, mcp_config)
            outcome = _run_attempt(cmd, document, send_document, timeout_s, run_id, trace_name, env, attempt)
        else:
            outcome = codex.attempt(message, document, send_document, resume_session_id, attempt, timeout_s)
            if outcome.session_token_usage is None:
                usage_complete = False
            else:
                previous = token_reported.get(outcome.session_id, {})
                try:
                    delta = _usage_delta(outcome.session_token_usage, previous)
                except ValueError as error:
                    usage_complete = False
                    outcome.kind, outcome.error, outcome.result_text = "usage_error", str(error), None
                else:
                    for key, amount in delta.items():
                        token_total[key] = token_total.get(key, 0) + amount
                    token_reported[outcome.session_id] = outcome.session_token_usage

        session_id = outcome.session_id or session_id
        attempt_cost = outcome.cost_usd
        if resume_session_id is not None and outcome.session_id == resume_session_id:
            attempt_cost = max(0.0, outcome.cost_usd - reported.get(resume_session_id, 0.0))
        if outcome.session_id:
            reported[outcome.session_id] = outcome.cost_usd
        cost_total += attempt_cost
        permission_denials_total.extend(outcome.permission_denials)
        if outcome.permission_denials:
            logger.warning(
                "call: artifact=%s attempt=%d permission_denials=%s",
                trace_name, attempt, outcome.permission_denials,
            )

        if outcome.kind != "ok":
            # Сбой процесса/API (таймаут, ненулевой код, битый JSON верхнего уровня,
            # нет события result, is_error) -> полный повтор без --resume (помощник — с --resume своей сессии)
            last_error = outcome.error
            _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                         attempt_cost, outcome.num_turns, outcome.kind, run_id=run_id, error=last_error)
            resume_session_id, message, send_document = base_resume, instruction, base_resume is None
            if _budget_exhausted(cost_total, max_budget_usd):
                break
            continue

        # Первый ответ модели; битый — чинится по схеме. Не подошёл (не чинится или не проходит контракт) —
        # ответ, принятый CLI после переписывания (15.10: иначе лишний повтор через --resume). Без частичных
        # сообщений — сразу принятый CLI
        answers = [a for a in (outcome.first_answer, outcome.result_text) if a is not None] or [None]
        if len(answers) == 2 and answers[0] == answers[1]:
            answers = answers[:1]
        value, failures = None, []
        for answer in answers:
            value, kind, error = _accept_answer(answer, schema, model_cls, validation_context)
            if (value is not None and failures and not _is_json(answers[0])
                    and len(answer) < MIN_FALLBACK_SHARE * len(answers[0])):
                # Первый ответ — битый JSON (CLI его отверг, модель переписала), а переписанный сильно короче —
                # урезан (паспорт 13.10: 16,6 → 8,1 тыс.): не берём. Первый — целый JSON, отвергнутый за схему
                # (длинные выжимки), — сокращение законно, берём
                value, kind, error = None, "answer_truncated", (
                    f"исправленный ответ урезан: {len(answer)} знаков против {len(answers[0])} в первом ответе — "
                    "верни ответ полностью, ничего не сокращая")
            if value is not None:
                if isinstance(answer, str) and not _is_json(answer):
                    logger.warning("call: artifact=%s attempt=%d — JSON ответа починен по схеме", trace_name, attempt)
                    log_event(run_id, "json_repaired", who=_agent_label(trace_name), trace=trace_name,
                              attempt=attempt)
                if failures:
                    logger.warning("call: artifact=%s attempt=%d — первый ответ не подошёл (%s), принят ответ CLI",
                                   trace_name, attempt, failures[0][0])
                    log_event(run_id, "first_answer_rejected", who=_agent_label(trace_name), trace=trace_name,
                              attempt=attempt, reason=failures[0][0], error=failures[0][1][:500])
                break
            failures.append((kind, error))

        if value is None:
            # Модели при повторе — ошибка последнего проверенного ответа: сессия CLI кончилась на нём; если тот
            # отбракован как урезанный — и ошибка первого ответа, иначе модель повторит её
            kind, last_error = failures[-1]
            if backend == "codex":
                last_error = f"{kind}: {last_error}"
            if kind == "answer_truncated":
                last_error = f"{failures[0][1]}\n\n{last_error}"
            _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                         attempt_cost, outcome.num_turns, kind, run_id=run_id, error=last_error)
            if outcome.session_id is not None:
                resume_session_id, message, send_document = (
                    outcome.session_id, _retry_message(last_error), False
                )
            else:
                # Защита: без session_id resume невозможен — начинаем заново
                resume_session_id, message, send_document = base_resume, instruction, base_resume is None
            if _budget_exhausted(cost_total, max_budget_usd):
                break
            continue

        # Успех
        last_error = None
        _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                     attempt_cost, outcome.num_turns, "ok", run_id=run_id, error=last_error)
        break

    duration_total = time.monotonic() - started
    result = CallResult(
        value=value,
        ok=value is not None,
        attempts=attempts_made,
        cost_usd=cost_total,
        session_id=session_id,
        error=last_error,
        permission_denials=permission_denials_total,
        duration_s=duration_total,
        session_cost_usd=reported.get(session_id, 0.0) if session_id else 0.0,
        backend=backend, model=model, cost_known=backend == "claude",
        token_usage=token_total or None,
        session_token_usage=token_reported.get(session_id), usage_complete=usage_complete,
    )
    if backend == "codex":
        record_codex_usage(run_id, token_total, usage_complete)
    _log_agent_finished(run_id, trace_name, result)
    return result


class _TextReply(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(min_length=1, description="Полный текст ответа на переданное задание.")


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
    if config.LLM_BACKEND == "codex":
        result = call(instruction + "\nВерни полный текст ответа в поле text схемы.", document, _TextReply,
                      run_id=run_id, trace_name=trace_name, allowed_tools=allowed_tools,
                      max_attempts=max_attempts, timeout_s=timeout_s, max_budget_usd=max_budget_usd)
        return replace(result, value=result.value.text if result.value is not None else None)
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

    _log_agent_started(run_id, trace_name, MODEL, allowed_tools, None, timeout_s)
    for attempt in range(1, max_attempts + 1):
        attempts_made = attempt

        cmd = _build_cmd(instruction, None, allowed_tools, None, max_budget_usd)
        outcome = _run_attempt(cmd, document, True, timeout_s, run_id, trace_name, attempt=attempt)

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
                         outcome.cost_usd, outcome.num_turns, outcome.kind, run_id=run_id, error=last_error)
            if _budget_exhausted(cost_total, max_budget_usd):
                break
            continue

        if outcome.result_text is None or not outcome.result_text.strip():
            last_error = "пустой ответ модели"
            _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                         outcome.cost_usd, outcome.num_turns, "empty_response", run_id=run_id, error=last_error)
            if _budget_exhausted(cost_total, max_budget_usd):
                break
            continue

        # Успех
        value = outcome.result_text
        last_error = None
        _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                     outcome.cost_usd, outcome.num_turns, "ok", run_id=run_id, error=last_error)
        break

    duration_total = time.monotonic() - started
    result = CallResult(
        value=value,
        ok=value is not None,
        attempts=attempts_made,
        cost_usd=cost_total,
        session_id=session_id,
        error=last_error,
        permission_denials=permission_denials_total,
        duration_s=duration_total,
        model=MODEL,
    )
    _log_agent_finished(run_id, trace_name, result)
    return result
