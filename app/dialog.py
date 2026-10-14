"""
Один ход диалога с результатом прогона — помощник после прогона (план итерации 3, шаг 4; итерация 4, 14.10,
`4_документы/План_помощника_итерации_4.md`, шаг 4).

Помощник — вызов runner.call по контракту DialogTurn: ответ пользователю и план из закрытого списка операций.
Сам ничего не меняет — план выполняет app/operations.py после подтверждения пользователя.

Сессия с памятью (14.10). Один разговор CLI на прогон, номер — в dialog/session.json:
  - первый ход (или сессии нет / продолжить не удалось) — новая сессия: системный промпт = промпт 07 + карта
    прогона (app/run_map.py) + акт + паспорт; вход = служебная строка о версии + последние HISTORY_MESSAGES реплик
    чата + сообщение;
  - следующие ходы — --resume той же сессии: вход = служебная строка + сообщение. Служебная строка говорит, какая
    версия сейчас и что изменилось с прошлого хода (из versions/journal.jsonl): модель помнит прочитанное раньше,
    а после правки должна знать, что файлы уже другие.
Вердикты, карту, бланк помощник читает сам инструментом app/mcp_run.py — только файлы своего прогона (конфиг
серверов — dialog/mcp.json). Конец сессии — конец работы с прогоном (app/sessions.py): после закрытия ходов нет.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from dataclasses import dataclass, field

from app import garant_limits, versions
from app.config import ALLOWED_TOOLS_WEB, DIALOG_MODEL, GARANT_ALL_TOOLS, GARANT_MCP_CONFIG, PROJECT_ROOT, PROMPT_DIALOG
from app.contracts import DialogTurn, Operation, Remark
from app.nodes.hypotheses import _add_cost_to_meta
from app.run_map import build_run_map
from app.runner import call
from app.trace import log_event, run_dir, save_bytes

CHAT = "dialog/chat.jsonl"
REMARKS = "dialog/remarks.json"   # очередь замечаний из файла эксперта
STATUS = "dialog/status.json"     # что идёт по прогону прямо сейчас; нет файла — ничего
# Процесс сервиса, записавший состояние: состояние от прежнего процесса — действие прервано перезапуском
SERVER_ID = f"{os.getpid()}-{int(time.time())}"
HISTORY_MESSAGES = 16       # сколько последних реплик чата уходит модели в начале новой сессии
# Сессия CLI помощника: session_id, started, last_turn, version_seen, cost_reported (стоимость сессии, как её
# сообщил CLI в прошлом ходе: при --resume он сообщает сумму с начала сессии — runner вычитает её)
SESSION = "dialog/session.json"
MCP_CONFIG = "dialog/mcp.json"    # серверы MCP помощника: Гарант + чтение артефактов прогона
# Инструменты чтения артефактов прогона (app/mcp_run.py, сервер p10run в dialog/mcp.json)
RUN_TOOLS = ["mcp__p10run__list_artifacts", "mcp__p10run__read_artifact"]
# Гарант в чате (решение владельца 14.10): все инструменты всегда, и лимитированные. Набор за сессию не меняется —
# смена инструментов посреди сессии сбрасывает кэш. Выгрузки (30 в месяц на всех) помощник делает сам и предупреждает:
# пишет в ответе, а после хода с выгрузкой сервис дописывает в чат «потрачено N, осталось M» (_report_exports)
GARANT_TOOLS = list(GARANT_ALL_TOOLS)
# Задание первого хода новой сессии; роль и правила — в системном промпте (промпт 07)
NEW_SESSION_INSTRUCTION = "Ответь на сообщение пользователя — оно последним блоком входа."
_ROLES = ("пользователь", "ассистент", "сервис")
# Кто в журнале сессии (events.jsonl) для каждой роли чата
_CHAT_WHO = {"пользователь": "пользователь", "ассистент": "помощник", "сервис": "система"}


@dataclass
class TurnResult:
    reply: str
    plan: list[Operation] = field(default_factory=list)
    cost_usd: float = 0.0
    sources: list[str] = field(default_factory=list)


def read_chat(run_id: str) -> list[dict]:
    path = run_dir(run_id) / CHAT
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def append_chat(run_id: str, role: str, text: str, **extra) -> dict:
    """Дописать реплику в чат прогона: пользователь, ассистент или сервис (что применено, что отклонено)."""
    assert role in _ROLES, role
    extra = {k: v for k, v in extra.items() if v is not None}   # пустые поля (нет сессии, нет цены) не пишутся
    entry = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "role": role, "text": text, **extra}
    path = run_dir(run_id) / CHAT
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # Журнал сессии: роль и начало текста (полный текст — в chat.jsonl)
    log_event(run_id, "chat", who=_CHAT_WHO[role], role=role, text=text[:300],
              plan_steps=len(extra["plan"]) if "plan" in extra else None,
              **{k: v for k, v in extra.items() if k in ("cost_usd", "session", "mark", "version")})
    return entry


def _json_block(title: str, raw: str) -> str:
    return f"## {title}\n\n```json\n{raw.strip()}\n```\n"


def shared_context(run_id: str) -> str:
    """
    Системный промпт сессии помощника (кроме короткого служебного — его добавляет runner): промпт 07, карта
    прогона, акт, паспорт. За жизнь прогона не меняется — каждый ход читает его из кэша (2.6), при --resume
    CLI берёт записанный в сессии.
    """
    root = run_dir(run_id)
    parts = [PROMPT_DIALOG.read_text(encoding="utf-8").rstrip() + "\n", build_run_map(),
             f"## ПОЛНЫЙ ТЕКСТ АКТА\n\n{(root / '01_document.md').read_text(encoding='utf-8')}\n"]
    if (root / "02_passport.json").is_file():
        parts.append(_json_block("ПАСПОРТ АКТА (JSON)", (root / "02_passport.json").read_text(encoding="utf-8")))
    return "\n".join(parts)


def mcp_config(run_id: str) -> str:
    """
    Файл серверов MCP помощника в папке прогона: Гарант (как в пайплайне, токен — из окружения) и p10run —
    чтение артефактов ЭТОГО прогона (app/mcp_run.py, папка — в P10_RUN_DIR). Выход: путь файла.
    """
    servers = json.loads(GARANT_MCP_CONFIG.read_text(encoding="utf-8"))["mcpServers"]
    servers["p10run"] = {"type": "stdio", "command": sys.executable,
                         "args": [str(PROJECT_ROOT / "app" / "mcp_run.py")],
                         "env": {"P10_RUN_DIR": str(run_dir(run_id).resolve())}}
    data = json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2).encode("utf-8")
    return str(save_bytes(run_id, MCP_CONFIG, data))


def load_session(run_id: str) -> dict | None:
    """Сессия CLI помощника по прогону; None — ещё не начиналась."""
    path = run_dir(run_id) / SESSION
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _save_session(run_id: str, data: dict) -> None:
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


def build_document(run_id: str, message: str, history: list[dict]) -> str:
    """Вход первого хода новой сессии (stdin): служебная строка, история чата, сообщение пользователя — последним.
    Акт, паспорт, карта прогона — в системном промпте; вердикты, карту, бланк помощник читает инструментом."""
    lines = [f"{m['role']}: {m['text']}" for m in history] or ["(начало диалога)"]
    return "\n".join([
        f"## СЛУЖЕБНО\n\n{version_line(run_id, None)}\n",
        "## ИСТОРИЯ ДИАЛОГА\n\n" + "\n\n".join(lines) + "\n",
        f"## СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ\n\n{message}\n",
    ])


def resume_message(run_id: str, message: str, version_seen: int) -> str:
    """Вход следующего хода той же сессии: служебная строка о версии и сообщение пользователя."""
    return f"## СЛУЖЕБНО\n\n{version_line(run_id, version_seen)}\n\n## СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ\n\n{message}\n"


def turn(run_id: str, message: str, *, session: str | None = None, timeout_s: int = 900) -> TurnResult:
    """
    Ответить на сообщение пользователя: текст и план. Реплики и стоимость — в чат и meta; session — чья реплика
    (сессия браузера). Есть сессия CLI — продолжаем её; продолжить не удалось — новая сессия с историей чата.
    """
    versions.recover(run_id)
    versions.ensure_v0(run_id)
    history = read_chat(run_id)[-HISTORY_MESSAGES:]
    n = len(read_chat(run_id)) + 1
    append_chat(run_id, "пользователь", message, session=session)
    # Инструменты хода: интернет, весь Гарант, чтение артефактов; остаток выгрузок — до хода (запрос бесплатный)
    mcp_tools = GARANT_TOOLS + RUN_TOOLS
    exports_before = (garant_limits.limits(fresh=True) or {}).get(garant_limits.EXPORT)
    common = dict(run_id=run_id, allowed_tools=ALLOWED_TOOLS_WEB, timeout_s=timeout_s, model=DIALOG_MODEL,
                  shared_context=shared_context(run_id), mcp_tools=mcp_tools, mcp_config=mcp_config(run_id),
                  short_cache_ttl=False)   # срок кэша 1 час: человек думает между сообщениями дольше 5 минут
    cli = load_session(run_id)
    cost = 0.0
    result = None
    if cli is not None:
        # Продолжение разговора: модель помнит прошлые ходы, служебная строка — что изменилось
        result = call(resume_message(run_id, message, cli["version_seen"]), None, DialogTurn,
                      trace_name=f"dialog/turns/{n:04d}.raw.json", resume_session_id=cli["session_id"],
                      resume_cost_base=float(cli.get("cost_reported", 0.0)), **common)
        cost += result.cost_usd
        if result.ok and result.value is not None:
            log_event(run_id, "dialog_session_resumed", session_id=cli["session_id"])
        else:
            log_event(run_id, "dialog_session_lost", session_id=cli["session_id"], error=(result.error or "")[:300])
            (run_dir(run_id) / SESSION).unlink(missing_ok=True)   # мёртвую сессию больше не продолжаем
            cli = None
    if cli is None:
        # Новая сессия: история чата — во входе, всё остальное — в системном промпте и инструментах
        result = call(NEW_SESSION_INSTRUCTION, build_document(run_id, message, history), DialogTurn,
                      trace_name=f"dialog/turns/{n:04d}{'.new' if result is not None else ''}.raw.json", **common)
        cost += result.cost_usd
        if result.ok and result.value is not None and result.session_id:
            cli = {"session_id": result.session_id, "started": _now()}
            log_event(run_id, "dialog_session_started", session_id=result.session_id)
    _add_cost_to_meta(run_id, cost)
    state = versions.load_state(run_id)
    current = state.current if state is not None else 0
    ok = result.ok and result.value is not None
    log_event(run_id, "dialog_turn", ok=ok, version=current, cost_usd=round(cost, 4),
              session_id=cli["session_id"] if cli else None)
    if not ok:
        reply = f"Не получилось ответить: {result.error or 'неизвестная ошибка'}. Попробуйте ещё раз."
        append_chat(run_id, "ассистент", reply, cost_usd=round(cost, 4), mark="сбой")
        out = TurnResult(reply=reply, cost_usd=cost)
    else:
        if cli is not None:
            _save_session(run_id, {**cli, "last_turn": _now(), "version_seen": current,
                                   "cost_reported": result.session_cost_usd})
        value = result.value
        append_chat(run_id, "ассистент", value.reply, plan=[op.model_dump(mode="json") for op in value.plan],
                    sources=value.sources or None, steps=turn_steps(run_id, n) or None, cost_usd=round(cost, 4))
        out = TurnResult(reply=value.reply, plan=list(value.plan), cost_usd=cost, sources=list(value.sources))
    _report_exports(run_id, exports_before, session)   # строка о выгрузках — после ответа помощника, если тратил
    return out


# Шаги помощника для пользователя (шаг 7): вызов инструмента → строка «что делаю»
_STEP_TITLES = {
    "mcp__p10run__list_artifacts": "смотрю список файлов результата",
    "mcp__garant__garant_document_redactions": "смотрю редакции акта в Гаранте",
    "mcp__garant__garant_snippets": "ищу место в документе Гаранта",
    "mcp__garant__garant_sutyazhnik": "ищу судебную практику в Гаранте («Сутяжник»)",
    "mcp__garant__garant_limits": "смотрю остаток лимитов Гаранта",
    "mcp__garant__garant_export_block_html": "выгружаю текст пункта из Гаранта (платно)",
    "mcp__garant__garant_export_html": "выгружаю текст документа из Гаранта (платно)",
    "mcp__garant__garant_export_rtf": "выгружаю документ из Гаранта в RTF (платно)",
    "mcp__garant__garant_export_odt": "выгружаю документ из Гаранта в ODT (платно)",
    "mcp__garant__garant_export_pdf": "выгружаю документ из Гаранта в PDF (платно)",
    "mcp__garant__garant_export_image": "выгружаю картинку из Гаранта (платно)",
    "mcp__garant__garant_export_formula": "выгружаю формулу из Гаранта (платно)",
    "mcp__garant__garant_document_info": "смотрю карточку документа в Гаранте",
    "mcp__garant__garant_find_hyperlinks": "распознаю ссылки на акты в тексте (Гарант)",
    "mcp__garant__garant_find_modified": "проверяю в Гаранте, менялись ли документы",
    "mcp__garant__garant_block_changed": "проверяю в Гаранте, менялись ли пункты",
    "mcp__garant__garant_prime_categories": "смотрю разделы новостей Гаранта",
    "mcp__garant__garant_prime_news": "ищу новости в Гаранте",
}


def _step_text(name: str, args: dict) -> str | None:
    """Строка шага по вызову инструмента; None — служебный вызов (ответ по схеме), не показывается."""
    if name == "StructuredOutput":
        return None
    if name == "mcp__p10run__read_artifact":
        query = args.get("query")
        return f"читаю {args.get('path', '')}" + (f" — ищу «{query}»" if query else "")
    if name == "mcp__garant__garant_search":
        return f"ищу в Гаранте: {args.get('query') or args.get('text') or ''}".rstrip(": ")
    if name == "WebSearch":
        return f"ищу в интернете: {args.get('query', '')}"
    if name == "WebFetch":
        return f"открываю {args.get('url', '')}"
    return _STEP_TITLES.get(name, f"вызываю {name.removeprefix('mcp__')}")


def turn_steps(run_id: str, n: int) -> list[str]:
    """
    Шаги хода n по порядку — из файлов хода dialog/turns/NNNN*.stream.jsonl (их пишет runner по ходу работы).
    Вход: прогон, номер хода (номер реплики пользователя в чате). Выход: строки «что делает помощник».
    """
    steps: list[str] = []
    seen: set[str] = set()
    for path in sorted((run_dir(run_id) / "dialog" / "turns").glob(f"{n:04d}*.stream.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line).get("event") or {}
            except (ValueError, AttributeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("id") in seen:
                    continue
                seen.add(block.get("id"))
                text = _step_text(block.get("name", ""), block.get("input") or {})
                if text:
                    steps.append(text)
    return steps


def _report_exports(run_id: str, before: int | None, session: str | None) -> None:
    """После хода, в котором помощник потратил выгрузки Гаранта, — строка в чат: сколько потрачено и осталось.
    Не тратил или остаток узнать не удалось — строки нет (выгрузки видно и в шагах хода)."""
    after = (garant_limits.limits(fresh=True) or {}).get(garant_limits.EXPORT)
    if before is None or after is None or before <= after:
        return
    append_chat(run_id, "сервис", f"Выгрузок Гаранта потрачено: {before - after}, осталось {after} в этом месяце "
                f"(на всех пользователей).", mark="лимиты", session=session)
    log_event(run_id, "garant_exports_spent", spent=before - after, left=after)


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
    data = {"state": state, "title": title, "detail": detail,
            "since": datetime.datetime.now().isoformat(timespec="seconds"), "server": SERVER_ID}
    save_bytes(run_id, STATUS, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def read_status(run_id: str) -> dict | None:
    path = run_dir(run_id) / STATUS
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):   # файл пишется атомарно, но страница не должна падать из-за него
        return None


def clear_status(run_id: str) -> None:
    (run_dir(run_id) / STATUS).unlink(missing_ok=True)
