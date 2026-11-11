"""
Один ход диалога с результатом прогона — помощник после прогона (план итерации 3, шаг 4; итерация 4, 14.10,
`4_документы/План_помощника_итерации_4.md`, шаг 4).

Помощник — вызов runner.call по контракту DialogTurn: ответ пользователю и план из закрытого списка операций.
Сам ничего не меняет — план выполняет app/operations.py после подтверждения пользователя.

Сессия с памятью (14.10). Один разговор CLI на прогон, номер — в dialog/session.json:
  - первый ход (или сессии нет / продолжить не удалось) — новая сессия: системный промпт = промпт 07 + карта
    прогона (app/run_map.py); вход = служебная строка о версии + последние HISTORY_MESSAGES реплик
    чата + сообщение;
  - следующие ходы — --resume той же сессии: вход = служебная строка + сообщение. Служебная строка говорит, какая
    версия сейчас и что изменилось с прошлого хода (из versions/journal.jsonl): модель помнит прочитанное раньше,
    а после правки должна знать, что файлы уже другие.
Вердикты, карту, бланк помощник читает сам инструментом app/mcp_run.py — только файлы своего прогона (конфиг
серверов — dialog/mcp.json). Конец сессии — конец работы с прогоном (app/sessions.py): после закрытия ходов нет.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field

from app import versions, config
from app.chat_common import (
    CHAT_WHO, GARANT_TOOLS, NEW_SESSION_INSTRUCTION, SESSION_GONE, append_chat_file, read_chat_file,
    tool_step_text, turn_tools,
)
from app.config import (
    ALLOWED_TOOLS_WEB, DIALOG_MODEL, GARANT_MCP_CONFIG, HISTORY_MESSAGES, PROJECT_ROOT, PROMPT_DIALOG, RESUME_FAILS_TO_RESET,
)
from app.chat_common import ChatUsage, archive_cli_session, cli_result_fields, resume_arguments, session_reset_reason
from app.contracts import ChatEntry, CliSession, DialogStatus, DialogTurn, Operation, Remark
from app.run_map import build_run_map
from app.runner import call, garant_available
from app.trace import DIALOG_STATUS_FILE, add_cost, log_event, run_dir, save_bytes

CHAT = "dialog/chat.jsonl"
REMARKS = "dialog/remarks.json"   # очередь замечаний из файла эксперта
# Процесс сервиса, записавший состояние: состояние от прежнего процесса — действие прервано перезапуском
SERVER_ID = f"{os.getpid()}-{int(time.time())}"
# Сессия CLI помощника (контракт CliSession): номер, когда начата, последний ход, какую версию видела модель,
# стоимость сессии, как её сообщил CLI в прошлом ходе (при --resume он сообщает сумму с начала сессии — runner вычитает её)
SESSION = "dialog/session.json"
MCP_CONFIG = "dialog/mcp.json"    # серверы MCP помощника: Гарант + чтение артефактов прогона
# Инструменты чтения артефактов прогона (app/mcp_run.py, сервер p10run в dialog/mcp.json)
RUN_TOOLS = ["mcp__p10run__list_artifacts", "mcp__p10run__read_artifact"]
WEB_TOOL = "mcp__garantweb__garant_web"
# Гарант в чате — все инструменты всегда (chat_common.GARANT_TOOLS). Сколько выгрузок потрачено — в журнал прогона
# (_log_exports). 27.10: строку об этом в чат эксперту больше не пишем — лимиты Гаранта эксперту не показываем;
# остаток виден на странице Гаранта-помощника


@dataclass
class TurnResult:
    reply: str
    plan: list[Operation] = field(default_factory=list)
    cost_usd: float = 0.0
    cost_known: bool = True
    sources: list[str] = field(default_factory=list)


def read_chat(run_id: str) -> list[ChatEntry]:
    return read_chat_file(run_dir(run_id) / CHAT)


def append_chat(run_id: str, role: str, text: str, **extra) -> ChatEntry:
    """Дописать реплику в чат прогона: пользователь, ассистент или сервис (что применено, что отклонено)."""
    entry = append_chat_file(run_dir(run_id) / CHAT, role, text, **extra)
    extra = {k: v for k, v in extra.items() if v is not None}
    # Журнал сессии: роль и начало текста (полный текст — в chat.jsonl)
    log_event(run_id, "chat", who=CHAT_WHO[role], role=role, text=text[:300],
              plan_steps=len(extra["plan"]) if "plan" in extra else None,
              **{k: v for k, v in extra.items() if k in ("cost_usd", "cost_known", "backend", "token_usage", "usage_complete", "session", "mark", "version")})
    return entry


def shared_context(run_id: str) -> str:
    """
    Инструкция и карта артефактов. Акт, паспорт и результаты читаются инструментом по мере необходимости.
    При --resume CLI берёт записанный контекст сессии.
    """
    return "\n".join([PROMPT_DIALOG.read_text(encoding="utf-8").rstrip() + "\n", build_run_map()])


def context_revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mcp_config(run_id: str, with_garant: bool = True) -> str:
    """
    Файл серверов MCP помощника в папке прогона: Гарант (как в пайплайне, токен — из окружения) и p10run —
    чтение артефактов ЭТОГО прогона (app/mcp_run.py, папка — в P10_RUN_DIR). Выход: путь файла.
    with_garant=False (15.10) — без Гаранта: файла токена нет, помощник работает без него.
    """
    servers = json.loads(GARANT_MCP_CONFIG.read_text(encoding="utf-8"))["mcpServers"] if with_garant else {}
    servers["p10run"] = {"type": "stdio", "command": sys.executable,
                         "args": [str(PROJECT_ROOT / "app" / "mcp_run.py")],
                         "env": {"P10_RUN_DIR": str(run_dir(run_id).resolve())}}
    servers["garantweb"] = {"type": "stdio", "command": sys.executable,
                            "args": [str(PROJECT_ROOT / "app" / "mcp_garant_web.py")],
                            "env": {"GARANT_TOKEN": ""}}
    data = json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2).encode("utf-8")
    return str(save_bytes(run_id, MCP_CONFIG, data))


def load_session(run_id: str) -> CliSession | None:
    """Сессия CLI помощника по прогону; None — ещё не начиналась."""
    path = run_dir(run_id) / SESSION
    return CliSession.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


def _save_session(run_id: str, session: CliSession) -> None:
    data = session.model_dump(mode="json", exclude_none=True)
    save_bytes(run_id, SESSION, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def version_line(run_id: str, version_seen: int | None) -> str:
    """
    Служебная строка хода: какая версия сейчас и что изменилось после version_seen (из журнала версий).
    version_seen=None — новая сессия: только номер версии.
    """
    state = versions.load_state(run_id)
    current = state.current if state is not None else 0
    line = f"Сейчас версия v{current}."
    if version_seen is None:
        return line
    changes = [f"v{e.version}: {e.action} — {e.target}" for e in versions.read_journal(run_id) if e.version > version_seen]
    return line + (" С прошлого хода изменено: " + "; ".join(changes) + "." if changes else " С прошлого хода изменений нет.")


def _service(run_id: str, version_seen: int | None, note: str) -> str:
    """Блок «СЛУЖЕБНО»: версия и что изменилось; note — пометка хода (15.10: Гарант недоступен)."""
    return f"## СЛУЖЕБНО\n\n{version_line(run_id, version_seen)}\n" + (f"\n{note}\n" if note else "")


def build_document(run_id: str, message: str, history: list[ChatEntry], note: str = "") -> str:
    """Вход первого хода новой сессии (stdin): служебная строка, история чата, сообщение пользователя — последним.
    Карта прогона — в инструкции; акт, паспорт и результаты помощник читает инструментом."""
    lines = [f"{m.role}: {m.text}" for m in history] or ["(начало диалога)"]
    return "\n".join([
        _service(run_id, None, note),
        "## ИСТОРИЯ ДИАЛОГА\n\n" + "\n\n".join(lines) + "\n",
        f"## СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ\n\n{message}\n",
    ])


def resume_message(run_id: str, message: str, version_seen: int, note: str = "") -> str:
    """Вход следующего хода той же сессии: служебная строка о версии и сообщение пользователя."""
    return f"{_service(run_id, version_seen, note)}\n## СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ\n\n{message}\n"


def turn(run_id: str, message: str, *, session: str | None = None, timeout_s: int = 900) -> TurnResult:
    """
    Ответить на сообщение пользователя: текст и план. Реплики и стоимость — в чат и meta; session — чья реплика
    (сессия браузера). Есть сессия CLI — продолжаем её; у CLI её больше нет — новая сессия с историей чата;
    временный сбой продолжения — ход «не получилось», сессия сохраняется (15.10).
    """
    from app.operations import recover_pending_plan
    recovered = recover_pending_plan(run_id)
    if recovered is not None:
        append_chat(run_id, "сервис", f"Прерванный план восстановлен: принятые шаги сохранены в версии {recovered}.", mark="частично")
    versions.recover(run_id)
    versions.ensure_v0(run_id)
    history = read_chat(run_id)[-HISTORY_MESSAGES:]
    n = len(read_chat(run_id)) + 1
    append_chat(run_id, "пользователь", message, session=session)
    # Инструменты хода: интернет, весь Гарант, чтение артефактов. Нет файла токена Гаранта (15.10) — ход без
    # Гаранта, а не сбой; пометка — в служебной строке входа (не репликой чата: та сбивает номер хода для живых
    # шагов и засоряет историю разговора), помощник скажет сам
    garant = garant_available()
    note = ("" if garant else "API Гаранта недоступен: нет токена. Интернет-версия может быть доступна через garant_web; "
                               "при ошибке инструмента сообщи об этом пользователю.")
    if not garant:
        log_event(run_id, "garant_unavailable", who="помощник", reason="нет рабочего файла токена Гаранта")
    mcp_tools = (GARANT_TOOLS if garant else []) + RUN_TOOLS + [WEB_TOOL]
    context = shared_context(run_id)
    revision = context_revision(context)
    common = dict(run_id=run_id, allowed_tools=ALLOWED_TOOLS_WEB, timeout_s=timeout_s, model=DIALOG_MODEL,
                  shared_context=context, mcp_tools=mcp_tools, mcp_config=mcp_config(run_id, garant),
                  short_cache_ttl=False)   # срок кэша 1 час: человек думает между сообщениями дольше 5 минут
    cli = load_session(run_id)
    reset_reason = (session_reset_reason(cli) or ("новая инструкция чата" if cli.context_revision != revision else None)) if cli else None
    if cli is not None and reset_reason:
        archive_cli_session(run_dir(run_id) / SESSION)
        log_event(run_id, "cli_session_reset", previous=cli.backend, backend=config.LLM_BACKEND, reason=reset_reason)
        cli = None
    cost = 0.0
    usage = ChatUsage()
    result = None
    if cli is not None:
        # Продолжение разговора: модель помнит прошлые ходы, служебная строка — что изменилось
        result = call(resume_message(run_id, message, cli.version_seen, note), None, DialogTurn,
                      trace_name=f"dialog/turns/{n:04d}.raw.json", resume_session_id=cli.session_id,
                      resume_cost_base=float(cli.cost_reported or 0.0), **resume_arguments(cli), **common)
        cost += result.cost_usd
        usage.add(result)
        if result.ok and result.value is not None:
            log_event(run_id, "dialog_session_resumed", session_id=cli.session_id)
        elif SESSION_GONE in (result.error or ""):
            log_event(run_id, "dialog_session_lost", session_id=cli.session_id, error=(result.error or "")[:300])
            archive_cli_session(run_dir(run_id) / SESSION)   # мёртвую сессию больше не продолжаем
            cli = None
        else:
            # 15.10 (код-ревью): сбой продолжения (таймаут, ошибка API) — память помощника не стираем: ход
            # «не получилось», следующий продолжит ту же сессию. Не продолжилась второй ход подряд — сессия сломана
            # (длинная или испорченная история): новая с историей чата (повторное ревью)
            fails = int(cli.resume_failures or 0) + 1
            if result.session_id == cli.session_id:
                cli = cli.model_copy(update=cli_result_fields(result))   # CLI считает сессию нарастающим итогом
            if fails >= RESUME_FAILS_TO_RESET:
                log_event(run_id, "dialog_session_lost", session_id=cli.session_id,
                          error=(result.error or "")[:300])
                archive_cli_session(run_dir(run_id) / SESSION)
                cli = None
            else:
                log_event(run_id, "dialog_resume_failed", session_id=cli.session_id,
                          error=(result.error or "")[:300])
                _save_session(run_id, cli.model_copy(update={**cli_result_fields(result), "resume_failures": fails}))
    if cli is None:
        # Новая сессия: история чата — во входе, всё остальное — в системном промпте и инструментах
        result = call(NEW_SESSION_INSTRUCTION, build_document(run_id, message, history, note), DialogTurn,
                      trace_name=f"dialog/turns/{n:04d}{'.new' if result is not None else ''}.raw.json", **common)
        cost += result.cost_usd
        usage.add(result)
        if result.ok and result.value is not None and result.session_id:
            cli = CliSession(session_id=result.session_id, started=_now(), context_revision=revision,
                             **cli_result_fields(result))
            log_event(run_id, "dialog_session_started", session_id=result.session_id)
    add_cost(run_id, cost)
    state = versions.load_state(run_id)
    current = state.current if state is not None else 0
    ok = result.ok and result.value is not None
    log_event(run_id, "dialog_turn", ok=ok, version=current, **usage.fields(),
              session_id=cli.session_id if cli else None)
    _log_exports(run_id, n)   # выгрузки Гаранта за ход — в журнал прогона
    if not ok:
        reply = f"Не получилось ответить: {result.error or 'неизвестная ошибка'}. Попробуйте ещё раз."
        append_chat(run_id, "ассистент", reply, **usage.fields(), mark="сбой")
        out = TurnResult(reply=reply, cost_usd=cost, cost_known=usage.cost_known)
    else:
        if cli is not None:
            _save_session(run_id, cli.model_copy(update={"last_turn": _now(), "version_seen": current,
                                                         **cli_result_fields(result), "resume_failures": 0}))
        value = result.value
        append_chat(run_id, "ассистент", value.reply, plan=[op.model_dump(mode="json") for op in value.plan],
                    sources=value.sources or None, steps=turn_steps(run_id, n) or None, **usage.fields())
        out = TurnResult(reply=value.reply, plan=list(value.plan), cost_usd=cost, cost_known=usage.cost_known, sources=list(value.sources))
    return out


def _turn_tools(run_id: str, n: int) -> tuple[list[dict], set[str]]:
    """Вызовы инструментов хода n и id вызовов с ошибкой — из dialog/turns/NNNN*.stream.jsonl (chat_common.turn_tools)."""
    return turn_tools(run_dir(run_id) / "dialog" / "turns", n)


def turn_steps(run_id: str, n: int) -> list[str]:
    """
    Шаги хода n по порядку — из файлов хода (_turn_tools).
    Вход: прогон, номер хода (номер реплики пользователя в чате). Выход: строки «что делает помощник».
    """
    uses, _ = _turn_tools(run_id, n)
    steps = [tool_step_text(u.get("name", ""), u.get("input") or {}) for u in uses]
    return [s for s in steps if s]


def _turn_exports(run_id: str, n: int) -> int:
    """Сколько выгрузок Гаранта сделал помощник в ходе n: удачные вызовы выгрузки (ошибка Гаранта — не трата)."""
    uses, errors = _turn_tools(run_id, n)
    return sum(1 for u in uses if u.get("status", "completed") == "completed" and str(u.get("name", "")).startswith("mcp__garant__garant_export")
               and str(u.get("id")) not in errors)


def _log_exports(run_id: str, n: int) -> None:
    """После хода, в котором помощник выгружал из Гаранта, — запись в журнал прогона: сколько потрачено.
    15.10 (код-ревью): «потрачено» — по вызовам выгрузки самого помощника в этом ходе (общий остаток тратят и
    параллельные прогоны). Не выгружал — записи нет. 27.10: в чат эксперту строку больше не пишем, остаток в журнал
    не запрашиваем — его никто не читал, а запрос к Гаранту шёл каждый такой ход."""
    spent = _turn_exports(run_id, n)
    if spent:
        log_event(run_id, "garant_exports_spent", spent=spent)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def save_remarks(run_id: str, file_name: str, remarks: list[Remark]) -> None:
    """Очередь замечаний из файла эксперта; новый файл заменяет прежнюю очередь."""
    data = {"file": file_name, "next": 0, "remarks": [r.model_dump(mode="json") for r in remarks]}
    save_bytes(run_id, REMARKS, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def _load_remarks(run_id: str) -> dict | None:
    path = run_dir(run_id) / REMARKS
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def remarks_progress(run_id: str) -> tuple[int, int] | None:
    """(сколько замечаний уже ушло в диалог, сколько всего); None — файла эксперта не было."""
    data = _load_remarks(run_id)
    return None if data is None else (data["next"], len(data["remarks"]))


def next_remark(run_id: str) -> Remark | None:
    """Взять следующее замечание и сдвинуть очередь; None — очередь разобрана."""
    data = _load_remarks(run_id)
    if data is None or data["next"] >= len(data["remarks"]):
        return None
    remark = Remark.model_validate(data["remarks"][data["next"]])
    data["next"] += 1
    save_bytes(run_id, REMARKS, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    return remark


def set_status(run_id: str, state: str, title: str, detail: str) -> None:
    """Что идёт по прогону: «думает» (ход помощника), «шаг» (шаг плана, возврат версии), «решение» (ждёт человека)."""
    status = DialogStatus(state=state, title=title, detail=detail,
                          since=datetime.datetime.now().isoformat(timespec="seconds"), server=SERVER_ID)
    save_bytes(run_id, DIALOG_STATUS_FILE, json.dumps(status.model_dump(mode="json"), ensure_ascii=False).encode("utf-8"))


def read_status(run_id: str) -> DialogStatus | None:
    path = run_dir(run_id) / DIALOG_STATUS_FILE
    try:
        return DialogStatus.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):   # файл пишется атомарно, но страница не должна падать из-за него (и неверный — тоже)
        return None


def clear_status(run_id: str) -> None:
    (run_dir(run_id) / DIALOG_STATUS_FILE).unlink(missing_ok=True)
