"""
Замечания пользователей о проблемах — кнопка «Сообщить о проблеме» в шапке (23.10, владелец: сбор ОС с платформы).

Человек выбирает, где проблема, и описывает её своими словами; где он был (страница, прогон или беседа)
и метку окна браузера сервис ставит сам. Каждое замечание — одна строка var/feedback/problems.jsonl с номером
по порядку; файл только дописывается. Раздел «Обратная связь» показывает все замечания и отдаёт их одной
таблицей Excel для разбора. Кто эксперт — позже (вход сейчас один на всех).
"""

from __future__ import annotations

import datetime
import io
import json
import threading

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

from app import config

PROBLEMS_FILE = "problems.jsonl"
# Где проблема — варианты формы; первый подходящий подставляется по странице (section_for_path)
SECTIONS = ("Результат анализа акта", "Чат по прогону", "Гарант-помощник", "Загрузка акта", "Другое")
_lock = threading.Lock()   # обработчики страницы пишут из разных потоков


def _path():
    return config.FEEDBACK_DIR / PROBLEMS_FILE


def section_for_path(path: str) -> str:
    """Раздел формы по адресу страницы: прогон — результат анализа, беседа — Гарант-помощник, главная — загрузка."""
    if path.startswith("/run/"):
        return SECTIONS[0]
    if path.startswith("/garant"):
        return SECTIONS[2]
    if path == "/":
        return SECTIONS[3]
    return SECTIONS[4]


def read_problems() -> list[dict]:
    """Все замечания по порядку записи; нет файла — пусто."""
    path = _path()
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def add_problem(section: str, text: str, *, page: str, session: str, run_id: str | None = None,
                act: str | None = None, chat_id: str | None = None) -> int:
    """Записать замечание; вернуть его номер. Пустой текст не записывается — ValueError."""
    text = text.strip()
    if not text:
        raise ValueError("пустое замечание")
    with _lock:
        num = len(read_problems()) + 1
        row = {"num": num, "at": datetime.datetime.now().isoformat(timespec="seconds"), "section": section,
               "text": text, "page": page, "run_id": run_id, "act": act, "chat_id": chat_id, "session": session}
        config.FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return num


def where_text(row: dict) -> str:
    """Где было: акт и прогон, беседа или просто страница."""
    if row.get("run_id"):
        return f"{row.get('act') or 'акт'} (прогон {row['run_id']})"
    if row.get("chat_id"):
        return f"беседа {row['chat_id']}"
    return row.get("page") or ""


def to_xlsx() -> bytes:
    """Все замечания одной таблицей Excel — для разбора."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Замечания"
    ws.append(["№", "Когда", "Где проблема", "Что не так", "Где был человек", "Страница", "Окно браузера"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for row in read_problems():
        ws.append([row["num"], row["at"].replace("T", " "), row["section"], row["text"], where_text(row),
                   row.get("page"), row.get("session")])
    for col, width in zip("ABCDEFG", (6, 18, 24, 80, 40, 30, 14)):
        ws.column_dimensions[col].width = width
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
