"""
Обёртка над `claude -p` — единая точка запуска модели для узлов пайплайна П-10.

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
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import (
    CLI_SETTINGS_FILE, EFFORT, GARANT_MAX_OUTPUT_TOKENS, GARANT_MCP_CONFIG, GARANT_NO_PROXY_HOST, GARANT_TOKEN_FILE, MODEL,
)
from app.json_fix import repair_json
from app.logger import get_run_logger
from app.trace import log_event, run_dir, save_bytes, save_raw

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
) -> list[str]:
    """
    Собрать argv для вызова `claude -p` (список аргументов, не строка через shell).

    schema_json=None — режим без --json-schema (текстовый ответ, call_text()).
    mcp_tools — разрешённые инструменты Гаранта; пусто/None — вызов без MCP, как раньше.
    """
    cmd = ["claude", "-p"]
    if resume_session_id is not None:
        cmd += ["--resume", resume_session_id]
    cmd.append(message)
    if schema_json is not None:
        cmd += ["--json-schema", schema_json]
    # Инструменты Гаранта одобряются по полным именам; встроенные (--tools) — как раньше
    approved = allowed_tools + [_GARANT_TOOL_PREFIX + t for t in (mcp_tools or [])]
    if schema_json is not None:
        cmd.append("--include-partial-messages")   # первый ответ модели целиком — см. _first_structured_answer
    cmd += [
        "--output-format", "stream-json", "--verbose",
        "--tools", ",".join(allowed_tools),         # доступны ровно встроенные инструменты узла
        "--allowedTools", " ".join(approved),       # и они же (плюс Гарант) одобрены без запроса
        *(_MCP_ISOLATION_FLAGS if mcp_tools else _ISOLATION_FLAGS),
        "--model", model,
        "--effort", EFFORT,
    ]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    if system_file is not None:
        cmd += ["--append-system-prompt-file", system_file]   # общий контекст группы агентов — в кэш
    return cmd


# Срок кэша 5 минут для вызовов пайплайна: чтения внутри прогона идут через секунды и минуты
_SHORT_CACHE_ENV = {"FORCE_PROMPT_CACHING_5M": "1"}

# Имя инструмента, через который CLI принимает ответ по --json-schema
_STRUCTURED_TOOL = "StructuredOutput"


def _first_structured_answer(events: list[Any]) -> str | None:
    """
    Полный текст ПЕРВОГО ответа модели в StructuredOutput — из частичных сообщений
    (--include-partial-messages). Битый JSON CLI отвергает, модель переписывает ответ
    и урезает содержимое (паспорт приютов: 16,6 тыс. символов → 8,1 тыс.), поэтому берётся
    первый ответ, а битый — чинится по схеме (app/json_fix.py). None — частичных сообщений нет.
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
    """Файл общего контекста в папке прогона — один на содержимое (одинаковый текст — один файл)."""
    data = shared_context.encode("utf-8")
    relname = f"_context/{hashlib.sha1(data).hexdigest()[:12]}.md"
    return str(save_bytes(run_id, relname, data))


def _mcp_env() -> dict[str, str]:
    """
    Окружение процесса агента с Гарантом: токен из файла машины и прокси из настроек CLI.
    Токен никуда не пишется — ни в команду, ни в лог, ни в трейс.
    """
    try:
        token = GARANT_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"нет файла с токеном Гаранта или он не читается ({GARANT_TOKEN_FILE})") from exc
    if not token or any(ch.isspace() for ch in token):
        raise RuntimeError(f"файл с токеном Гаранта пуст или испорчен ({GARANT_TOKEN_FILE})")
    env = {"GARANT_TOKEN": token, **_NO_AUTO_MEMORY_ENV}
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
        "call: artifact=%s attempt=%d/%d duration=%.1fs cost=$%.4f turns=%s outcome=%s",
        trace_name, attempt, max_attempts, duration_s, cost_usd, turns, outcome,
    )
    log_event(run_id, "agent_attempt", who=_agent_label(trace_name), trace=trace_name, attempt=attempt,
              max_attempts=max_attempts, outcome=outcome, duration_s=round(duration_s, 1), cost_usd=cost_usd,
              turns=num_turns, error=(error or "")[:500] or None)


def _agent_label(trace_name: str) -> str:
    """Кто в журнале сессии: «агент <артефакт без .raw.json>» — по нему видно узел и номер индикатора."""
    return "агент " + trace_name.removesuffix(".raw.json")


def _log_agent_started(run_id: str, trace_name: str, model: str, allowed_tools: list[str],
                       mcp_tools: list[str] | None, timeout_s: int) -> None:
    """Событие журнала: агент запущен — модель, встроенные инструменты, инструменты Гаранта, таймаут."""
    log_event(run_id, "agent_started", who=_agent_label(trace_name), trace=trace_name, model=model,
              tools=list(allowed_tools), garant=list(mcp_tools or []), timeout_s=timeout_s)


def _log_agent_finished(run_id: str, trace_name: str, result: "CallResult") -> None:
    """Событие журнала: итог вызова агента — успех, число попыток, стоимость, время, последняя ошибка."""
    log_event(run_id, "agent_finished", who=_agent_label(trace_name), trace=trace_name, ok=result.ok,
              attempts=result.attempts, cost_usd=result.cost_usd, duration_s=round(result.duration_s, 1),
              error=(result.error or "")[:500] or None)


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
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE if document is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
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
        raise subprocess.TimeoutExpired(cmd, timeout_s, output=_as_stdout(lines)) from None
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
    shared_context: str | None = None,
    short_cache_ttl: bool = True,
    mcp_tools: list[str] | None = None,
    validation_context: dict[str, Any] | None = None,
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

    mcp_tools — инструменты Гаранта, разрешённые агенту (см. «ГАРАНТ» в docstring модуля).

    Исключение бросается только если ретраем чинить бессмысленно: бинарь
    claude не найден, функция вызвана с неверными аргументами, либо агенту нужен
    Гарант, а файла токена нет или он испорчен (RuntimeError до запуска модели:
    тихо работать без Гаранта нельзя; узел превращает исключение в вердикт «ошибка»).
    Исчерпание попыток — НЕ исключение, а CallResult(ok=False, ...); решение,
    что делать дальше, принимает вызывающий узел.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts должен быть >= 1")

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

    system_file = _shared_context_file(run_id, shared_context) if shared_context is not None else None
    env = {**os.environ, **_SHORT_CACHE_ENV} if short_cache_ttl else None
    if mcp_tools:
        env = {**(env if env is not None else os.environ), **_mcp_env()}
    _log_agent_started(run_id, trace_name, model, allowed_tools, mcp_tools, timeout_s)

    # Состояние следующей попытки: что слать и слать ли документ в stdin
    message = instruction
    resume_session_id: str | None = None
    send_document = True

    for attempt in range(1, max_attempts + 1):
        attempts_made = attempt

        cmd = _build_cmd(message, schema_json, allowed_tools, resume_session_id, max_budget_usd, model, system_file,
                         mcp_tools)
        outcome = _run_attempt(cmd, document, send_document, timeout_s, run_id, trace_name, env, attempt)

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
                         outcome.cost_usd, outcome.num_turns, outcome.kind, run_id=run_id, error=last_error)
            resume_session_id, message, send_document = None, instruction, True
            if _budget_exhausted(cost_total, max_budget_usd):
                break
            continue

        # Первый ответ модели; битый — чинится по схеме. Без частичных сообщений — принятый CLI
        answer = outcome.first_answer if outcome.first_answer is not None else outcome.result_text
        try:
            parsed = _parse_answer(answer, schema)
        except (json.JSONDecodeError, TypeError) as exc:
            last_error = f"поле result не разбирается как JSON: {exc}"
            _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                         outcome.cost_usd, outcome.num_turns, "result_json_error", run_id=run_id, error=last_error)
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

        if isinstance(answer, str) and not _is_json(answer):
            logger.warning("call: artifact=%s attempt=%d — JSON ответа починен по схеме", trace_name, attempt)
            log_event(run_id, "json_repaired", who=_agent_label(trace_name), trace=trace_name, attempt=attempt)

        try:
            value = model_cls.model_validate(parsed, context=validation_context)
        except ValidationError as exc:
            last_error = str(exc)
            _log_attempt(logger, trace_name, attempt, max_attempts, outcome.duration_s,
                         outcome.cost_usd, outcome.num_turns, "validation_error", run_id=run_id, error=last_error)
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
    )
    _log_agent_finished(run_id, trace_name, result)
    return result


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
    )
    _log_agent_finished(run_id, trace_name, result)
    return result
