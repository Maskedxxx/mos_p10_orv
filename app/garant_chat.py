"""
Гарант-помощник — беседа эксперта с агентом по документу (платформа, шаги 3–4; владелец 23.10).

Эксперт прикладывает документ (.docx, .pdf, .doc) и задаёт вопросы. Агент ищет в Гаранте (все инструменты API
«Гарант Коннект») и в интернете, отвечает со ссылками на источники и по просьбе делает файлы на скачивание.
Беседы видят все пользователи (владелец 23.10: «пока что все, позже доработаем»).

Беседа = папка runs/garant/<chat_id>/ (run_id = "garant/<chat_id>"): папка без progress.json не попадает
ни в историю прогонов, ни в закрытие сессий по простою (app/sessions.py). Внутри:
  - chat.json     — название, кто и когда создал, приложенные документы;
  - chat.jsonl    — реплики: пользователь, ассистент, сервис;
  - docs/NN.<ext> и docs/NN.md — приложенный документ и его текст (шаг чтения сервиса, app/nodes/convert.py);
  - session.json  — сессия CLI для --resume: агент помнит разговор;
  - turns/        — сырые ответы и ход агента по каждому ходу (runner.call);
  - files/        — файлы, сделанные по просьбе пользователя;
  - events.jsonl  — журнал беседы (trace.log_event).

Ход — по образцу помощника после прогона (app/dialog.py: turn): runner.call с контрактом GarantTurn, память —
--resume. Тексты документов — в системном промпте сессии (shared_context); новый документ посреди беседы —
следующий ход начинает новую сессию CLI с историей чата. Нет файла токена Гаранта — ход без Гаранта, агент
скажет об этом сам.

Файлы делает КОД, не агент: у агента нет доступа к диску и командам сервера (на сервере токены и ключи).
Агент отдаёт содержимое (markdown или таблицу), код собирает docx (python-docx), xlsx (openpyxl), md, pdf
(docx → LibreOffice).

Одновременно идут не больше config.GARANT_MAX_PARALLEL_TURNS ходов на весь сервис — подписка одна на прогоны и беседы,
остальные ходы ждут очереди.
"""

from __future__ import annotations

import datetime
import json
import re
import secrets
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app import garant_limits, trace
from app.chat_common import (
    CHAT_WHO, GARANT_OFF_NOTE, GARANT_TOOLS, NEW_SESSION_INSTRUCTION, SESSION_GONE, append_chat_file, read_chat_file,
    tool_step_text, turn_tools,
)
from app.config import ALLOWED_TOOLS_WEB, DIALOG_MODEL, GARANT_MAX_PARALLEL_TURNS, HISTORY_MESSAGES, PROMPT_GARANT_CHAT
from app.contracts import (
    ChatEntry, CliSession, GarantChatInfo, GarantDocument, GarantFile, GarantTable, GarantTurn,
)
from app.nodes.convert import convert_to_markdown
from app.runner import call, garant_available
from app.trace import DOCUMENT_FILE, log_event, run_dir

CHATS = "garant"                 # папка бесед внутри runs/
CHAT_INFO = "chat.json"
CHAT = "chat.jsonl"
SESSION = "session.json"
FILES = "files"
DOCS = "docs"
FILE_FORMATS = ("docx", "xlsx", "md", "pdf")
_CHAT_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{6}$")

_SLOTS = threading.BoundedSemaphore(GARANT_MAX_PARALLEL_TURNS)
_BUSY: dict[str, str] = {}       # chat_id → «в очереди» / «ищет»: что идёт по беседе прямо сейчас (в этом процессе)
_BUSY_LOCK = threading.Lock()


@dataclass
class TurnResult:
    reply: str
    ok: bool
    cost_usd: float = 0.0
    files: list[dict] = field(default_factory=list)


# ---- Беседы: папки и реплики ----

def chat_run_id(chat_id: str) -> str:
    """run_id беседы для trace и runner: runs/garant/<chat_id>."""
    return f"{CHATS}/{chat_id}"


def valid_chat_id(chat_id: str) -> bool:
    """Номер беседы из адреса страницы: только формат сервиса и существующая папка — без выхода из runs/garant."""
    return bool(_CHAT_ID_RE.match(chat_id)) and (run_dir(chat_run_id(chat_id)) / CHAT_INFO).is_file()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def new_chat(session: str | None = None) -> str:
    """Создать беседу: папка и chat.json. Выход: номер беседы (дата, время и код, как у прогона)."""
    chat_id = f"{datetime.datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}"
    _write_json(run_dir(chat_run_id(chat_id)) / CHAT_INFO,
                GarantChatInfo(title="", created_at=_now(), created_by=session).model_dump(mode="json"))
    log_event(chat_run_id(chat_id), "garant_chat_created", who="пользователь", session=session)
    return chat_id


def chat_info(chat_id: str) -> GarantChatInfo:
    return GarantChatInfo.model_validate_json((run_dir(chat_run_id(chat_id)) / CHAT_INFO).read_text(encoding="utf-8"))


def _update_info(chat_id: str, **fields) -> None:
    info = GarantChatInfo.model_validate({**chat_info(chat_id).model_dump(), **fields})
    _write_json(run_dir(chat_run_id(chat_id)) / CHAT_INFO, info.model_dump(mode="json"))


def list_chats() -> list[dict]:
    """Все беседы, новые сверху: номер, название, кто и когда создал, сколько реплик и документов, стоимость."""
    root = trace.RUNS_DIR / CHATS   # через модуль trace — там же, где run_dir берёт папку прогонов
    if not root.is_dir():
        return []
    out = []
    for path in root.iterdir():
        if not (_CHAT_ID_RE.match(path.name) and (path / CHAT_INFO).is_file()):
            continue
        try:
            info = chat_info(path.name)
        except (OSError, ValueError):
            continue
        chat = read_chat(path.name)
        out.append({"chat_id": path.name, "title": info.title or "Без названия",
                    "created_at": info.created_at, "created_by": info.created_by,
                    "messages": sum(1 for m in chat if m.role != "сервис"),
                    "documents": len(info.documents),
                    "cost_usd": round(sum(m.cost_usd or 0.0 for m in chat), 4)})
    out.sort(key=lambda c: c["chat_id"], reverse=True)
    return out


def read_chat(chat_id: str) -> list[ChatEntry]:
    return read_chat_file(run_dir(chat_run_id(chat_id)) / CHAT)


def append_chat(chat_id: str, role: str, text: str, **extra) -> ChatEntry:
    """Дописать реплику беседы; пустые поля не пишутся. В журнал беседы — роль и начало текста."""
    entry = append_chat_file(run_dir(chat_run_id(chat_id)) / CHAT, role, text, **extra)
    extra = {k: v for k, v in extra.items() if v is not None}
    log_event(chat_run_id(chat_id), "chat", who=CHAT_WHO[role], role=role, text=text[:300],
              **{k: v for k, v in extra.items() if k in ("cost_usd", "session", "mark")})
    return entry


def busy_state(chat_id: str) -> str | None:
    """Что идёт по беседе: «в очереди», «ищет» или None — ничего."""
    with _BUSY_LOCK:
        return _BUSY.get(chat_id)


def _set_busy(chat_id: str, state: str | None) -> None:
    with _BUSY_LOCK:
        if state is None:
            _BUSY.pop(chat_id, None)
        else:
            _BUSY[chat_id] = state


# ---- Документы беседы ----

def add_document(chat_id: str, filename: str, data: bytes, session: str | None = None) -> int:
    """
    Приложить документ: сохранить, прочитать шагом чтения сервиса (как акт прогона), текст — в docs/NN.md.
    Следующий ход начинает новую сессию CLI: текст документов — в её системном промпте. Выход: число знаков.
    Не прочитался — ValueError с понятной причиной (страница покажет её пользователю).
    """
    rid = chat_run_id(chat_id)
    root = run_dir(rid)
    docs = list(chat_info(chat_id).documents)
    n = len(docs) + 1
    suffix = Path(filename).suffix.lower()
    src = root / DOCS / f"{n:02d}{suffix}"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    try:
        text = convert_to_markdown(src, rid)
    except Exception as error:   # повреждён, под паролем, пустой — человеку понятная причина, техника — в журнал
        log_event(rid, "garant_document_failed", file=filename, error=str(error)[:300])
        src.unlink(missing_ok=True)
        raise ValueError(f"Не удалось прочитать файл «{filename}»: возможно, он повреждён, защищён паролем "
                         "или это скан без текста. Пересохраните его в Word как .docx и загрузите снова.") from None
    (root / DOCS / f"{n:02d}.md").write_text(text, encoding="utf-8")
    (root / DOCUMENT_FILE).unlink(missing_ok=True)   # шаг чтения пишет его по привычке прогона — здесь не нужен
    docs.append(GarantDocument(n=n, name=filename, chars=len(text), at=_now()))
    info = chat_info(chat_id)
    _update_info(chat_id, documents=docs, title=info.title or Path(filename).stem)
    # Новый документ — в системном промпте новой сессии: старую больше не продолжаем
    (root / SESSION).unlink(missing_ok=True)
    append_chat(chat_id, "сервис", f"Документ «{filename}» приложен: {len(text):,} знаков текста.".replace(",", " "),
                mark="документ", session=session)
    log_event(rid, "garant_document_added", file=filename, chars=len(text))
    return len(text)


def shared_context(chat_id: str) -> str:
    """Системный промпт сессии: промпт 08 и тексты приложенных документов."""
    root = run_dir(chat_run_id(chat_id))
    parts = [PROMPT_GARANT_CHAT.read_text(encoding="utf-8").rstrip() + "\n"]
    docs = chat_info(chat_id).documents
    if not docs:
        parts.append("## ДОКУМЕНТЫ ПОЛЬЗОВАТЕЛЯ\n\nПользователь пока не приложил документов.\n")
    for d in docs:
        text = (root / DOCS / f"{d.n:02d}.md").read_text(encoding="utf-8")
        parts.append(f"## ДОКУМЕНТ ПОЛЬЗОВАТЕЛЯ {d.n}: «{d.name}»\n\n{text}\n")
    return "\n".join(parts)


# ---- Ход агента ----

def _load_session(chat_id: str) -> CliSession | None:
    path = run_dir(chat_run_id(chat_id)) / SESSION
    return CliSession.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


def build_document(message: str, history: list[ChatEntry], note: str = "") -> str:
    """Вход первого хода новой сессии: пометка (Гарант недоступен), история беседы, сообщение — последним."""
    lines = [f"{m.role}: {m.text}" for m in history] or ["(начало беседы)"]
    head = f"## СЛУЖЕБНО\n\n{note}\n\n" if note else ""
    return (head + "## ИСТОРИЯ БЕСЕДЫ\n\n" + "\n\n".join(lines) + "\n\n"
            + f"## СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ\n\n{message}\n")


def resume_message(message: str, note: str = "") -> str:
    """Вход следующего хода той же сессии: агент помнит разговор и документы."""
    head = f"## СЛУЖЕБНО\n\n{note}\n\n" if note else ""
    return f"{head}## СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ\n\n{message}\n"


def _turn_tools(chat_id: str, n: int) -> tuple[list[dict], set[str]]:
    """Вызовы инструментов хода n (без дублей) и id вызовов с ошибкой — из turns/NNNN*.stream.jsonl (chat_common)."""
    return turn_tools(run_dir(chat_run_id(chat_id)) / "turns", n)


def _report_exports(chat_id: str, n: int, session: str | None) -> None:
    """Ход с выгрузками Гаранта — строка в беседу: сколько потрачено и сколько осталось на всех в этом месяце."""
    uses, errors = _turn_tools(chat_id, n)
    spent = sum(1 for u in uses if str(u.get("name", "")).startswith("mcp__garant__garant_export")
                and str(u.get("id")) not in errors)
    if not spent:
        return
    left = (garant_limits.limits(fresh=True) or {}).get(garant_limits.EXPORT)
    tail = f", осталось {left} в этом месяце (на всех пользователей)" if left is not None else ""
    append_chat(chat_id, "сервис", f"Выгрузок Гаранта потрачено: {spent}{tail}.", mark="лимиты", session=session)


def turn(chat_id: str, message: str, *, session: str | None = None, timeout_s: int = 900) -> TurnResult:
    """
    Ответить на сообщение пользователя. Реплики, файлы и стоимость — в беседу. Есть сессия CLI — продолжаем её;
    не продолжилась (её нет у CLI или сбой) — новая сессия с историей беседы в этом же ходе.
    Ходов на весь сервис не больше GARANT_MAX_PARALLEL_TURNS — остальные ждут, беседа помечена «в очереди».
    """
    rid = chat_run_id(chat_id)
    history = read_chat(chat_id)[-HISTORY_MESSAGES:]
    n = sum(1 for m in read_chat(chat_id) if m.role == "пользователь") + 1
    append_chat(chat_id, "пользователь", message, session=session)
    if not chat_info(chat_id).title.strip():
        _update_info(chat_id, title=message.strip().splitlines()[0][:80])
    _set_busy(chat_id, "в очереди")
    try:
        with _SLOTS:
            _set_busy(chat_id, "ищет")
            return _turn(chat_id, rid, message, history, n, session, timeout_s)
    finally:
        _set_busy(chat_id, None)


def _turn(chat_id: str, rid: str, message: str, history: list[dict], n: int, session: str | None,
          timeout_s: int) -> TurnResult:
    garant = garant_available()
    note = "" if garant else GARANT_OFF_NOTE
    if not garant:
        log_event(rid, "garant_unavailable", who="помощник", reason="нет рабочего файла токена Гаранта")
    common = dict(run_id=rid, allowed_tools=ALLOWED_TOOLS_WEB, timeout_s=timeout_s, model=DIALOG_MODEL,
                  shared_context=shared_context(chat_id), mcp_tools=GARANT_TOOLS if garant else None,
                  short_cache_ttl=False)   # кэш 1 час: человек думает между сообщениями дольше 5 минут
    cli = _load_session(chat_id)
    cost = 0.0
    result = None
    if cli is not None:
        result = call(resume_message(message, note), None, GarantTurn, trace_name=f"turns/{n:04d}.raw.json",
                      resume_session_id=cli.session_id, resume_cost_base=float(cli.cost_reported or 0.0),
                      **common)
        cost += result.cost_usd
        if not (result.ok and result.value is not None):
            # Упрощённо (в отличие от помощника прогона): любая неудача продолжения — новая сессия с историей
            kind = "garant_session_lost" if SESSION_GONE in (result.error or "") else "garant_resume_failed"
            log_event(rid, kind, session_id=cli.session_id, error=(result.error or "")[:300])
            (run_dir(rid) / SESSION).unlink(missing_ok=True)
            cli = None
    if cli is None:
        result = call(NEW_SESSION_INSTRUCTION, build_document(message, history, note), GarantTurn,
                      trace_name=f"turns/{n:04d}{'.new' if result is not None else ''}.raw.json", **common)
        cost += result.cost_usd
        if result.ok and result.value is not None and result.session_id:
            cli = CliSession(session_id=result.session_id, started=_now())
            log_event(rid, "garant_session_started", session_id=result.session_id)
    ok = result.ok and result.value is not None
    log_event(rid, "garant_turn", ok=ok, cost_usd=round(cost, 4), session_id=cli.session_id if cli else None)
    _report_exports(chat_id, n, session)
    steps = [s for s in (tool_step_text(u.get("name", ""), u.get("input") or {}) for u in _turn_tools(chat_id, n)[0]) if s]
    if not ok:
        # Техническая причина — в журнал беседы; человеку — понятная фраза без английского
        log_event(rid, "garant_turn_failed", error=(result.error or "")[:500])
        reply = ("Не получилось ответить: помощник не уложился во время или сервис модели не ответил. "
                 "Попробуйте ещё раз.")
        append_chat(chat_id, "ассистент", reply, cost_usd=round(cost, 4), mark="сбой", steps=steps or None)
        return TurnResult(reply=reply, ok=False, cost_usd=cost)
    if cli is not None:
        _write_json(run_dir(rid) / SESSION, cli.model_copy(update={"last_turn": _now(), "cost_reported": result.session_cost_usd})
                    .model_dump(mode="json", exclude_none=True))
    value = result.value
    files, failed = [], []
    for spec in value.files:
        try:
            files.append(render_file(chat_id, spec))
        except Exception as error:   # файл не собрался — ответ всё равно показываем, о файле — строкой сервиса
            log_event(rid, "garant_file_failed", name=spec.name, format=spec.format, error=str(error)[:300])
            failed.append(f"{spec.name}.{spec.format}")
    append_chat(chat_id, "ассистент", value.reply, sources=value.sources or None, files=files or None,
                steps=steps or None, cost_usd=round(cost, 4))
    if failed:
        append_chat(chat_id, "сервис", "Не удалось собрать файл: " + ", ".join(failed)
                    + ". Попросите помощника сделать его ещё раз или в другом формате.", mark="сбой", session=session)
    return TurnResult(reply=value.reply, ok=True, cost_usd=cost, files=files)


# ---- Файлы на скачивание ----

def _safe_name(name: str) -> str:
    """Имя файла без пути и служебных знаков: буквы, цифры, пробел, точка, дефис, подчёркивание."""
    cleaned = re.sub(r"[^\w .\-]", "", name, flags=re.UNICODE).strip(" .")
    return (cleaned or "Файл")[:80]


def _free_path(folder: Path, stem: str, ext: str) -> Path:
    """Свободное имя в папке: «Справка.docx», «Справка (2).docx», …"""
    path = folder / f"{stem}.{ext}"
    i = 2
    while path.exists():
        path = folder / f"{stem} ({i}).{ext}"
        i += 1
    return path


def file_path(chat_id: str, relname: str) -> Path | None:
    """Путь файла беседы по его имени в chat.jsonl; только внутри files/ этой беседы, иначе None."""
    folder = (run_dir(chat_run_id(chat_id)) / FILES).resolve()
    path = (folder / relname).resolve()
    return path if path.parent == folder and path.is_file() else None


def render_file(chat_id: str, spec: GarantFile) -> dict:
    """Собрать файл по содержимому от агента в files/. Выход: {"name": имя для скачивания, "format": …}."""
    folder = run_dir(chat_run_id(chat_id)) / FILES
    folder.mkdir(parents=True, exist_ok=True)
    path = _free_path(folder, _safe_name(spec.name), spec.format)
    if spec.format == "md":
        path.write_text(spec.text or "", encoding="utf-8")
    elif spec.format == "docx":
        _markdown_to_docx(spec.text or "", path)
    elif spec.format == "xlsx":
        _table_to_xlsx(spec.table, path)
    elif spec.format == "pdf":
        _markdown_to_pdf(spec.text or "", path)
    log_event(chat_run_id(chat_id), "garant_file_made", name=path.name, format=spec.format)
    return {"name": path.name, "format": spec.format}


def _add_runs(paragraph, text: str) -> None:
    """Текст абзаца с выделением **жирным** — остальная разметка markdown внутри строки снимается."""
    for i, part in enumerate(re.split(r"\*\*", text)):
        if part:
            paragraph.add_run(part.replace("`", "")).bold = i % 2 == 1


def _markdown_to_docx(text: str, path: Path) -> None:
    """Простой markdown → Word: заголовки, списки, таблицы «| a | b |», абзацы."""
    from docx import Document

    doc = Document()
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if stripped.startswith("|"):
            # Таблица: подряд идущие строки «| … |», строка-разделитель «|---|» пропускается
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                width = max(len(r) for r in rows)
                table = doc.add_table(rows=len(rows), cols=width)
                table.style = "Table Grid"
                for r, cells in enumerate(rows):
                    for c, value in enumerate(cells):
                        _add_runs(table.cell(r, c).paragraphs[0], value)
                        if r == 0:
                            for run in table.cell(r, c).paragraphs[0].runs:
                                run.bold = True
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)", stripped)
        if heading:
            doc.add_heading(heading.group(2), level=len(heading.group(1)))
        elif re.match(r"^[-*]\s+", stripped):
            _add_runs(doc.add_paragraph(style="List Bullet"), re.sub(r"^[-*]\s+", "", stripped))
        elif re.match(r"^\d+[.)]\s+", stripped):
            _add_runs(doc.add_paragraph(style="List Number"), re.sub(r"^\d+[.)]\s+", "", stripped))
        elif stripped:
            _add_runs(doc.add_paragraph(), stripped)
        i += 1
    doc.save(str(path))


def _table_to_xlsx(table: GarantTable | None, path: Path) -> None:
    """Таблица → Excel: жирные заголовки, ширина столбцов по содержимому, перенос строк."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Таблица"
    headers = table.headers if table is not None else []
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in (table.rows if table is not None else []):
        ws.append(list(row))
    for col in ws.columns:
        width = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
        for c in col:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(str(path))


def _markdown_to_pdf(text: str, path: Path) -> None:
    """Markdown → Word → PDF через LibreOffice (без интерфейса). Не вышло — RuntimeError."""
    with tempfile.TemporaryDirectory() as tmp:
        docx_path = Path(tmp) / f"{path.stem}.docx"
        _markdown_to_docx(text, docx_path)
        done = subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(docx_path)],
                              capture_output=True, timeout=120)
        made = Path(tmp) / f"{path.stem}.pdf"
        if done.returncode != 0 or not made.is_file():
            raise RuntimeError(f"LibreOffice не собрал PDF (код {done.returncode})")
        path.write_bytes(made.read_bytes())
