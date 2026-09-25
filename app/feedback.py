"""
Разбор заполненного экспертом Excel (план итерации 3, шаг 5).

Эксперт заполняет оранжевые колонки книги прогона: на листе «Вердикты» —
«Согласны?», «Если нет — какой верный», «Замечание»; на карте гипотез и в запросе
показателей — «Оценка (1–5)» и «Замечание». read_remarks возвращает все
заполненные строки. В очередь диалога идут несогласия и замечания с текстом
(is_queued); оценки без текста — одной сводкой (summary). Каждое замечание
из очереди — один ход диалога: remark_message делает из него сообщение.

Шапки листов ищутся по заголовкам, не по координатам: строка шапки и колонки
сдвигаются (строка реквизитов акта, подпись листа, колонка «№»).
"""

from __future__ import annotations

import re
from pathlib import Path

from openpyxl import load_workbook

from app.contracts import Remark
from app.nodes.excel import (
    _EXPERT_SCORE_COLUMNS,
    _EXPERT_VERDICT_COLUMNS,
    _SHEET_MAP,
    _SHEET_REQUEST,
    _SHEET_VERDICTS,
)

_AGREE, _CORRECT, _TEXT = _EXPERT_VERDICT_COLUMNS[0]
_SCORE = _EXPERT_SCORE_COLUMNS[0][0]
_TITLE_LEN = 150

# лист → (заголовок, по которому ищется шапка; колонка ключа; колонка «о чём строка»)
_SHEETS = {
    _SHEET_VERDICTS: ("Индикатор", "Индикатор", "Индикатор"),
    _SHEET_MAP: ("Индикаторы", "№", "Гипотеза"),
    _SHEET_REQUEST: ("Показатель", "№", "Показатель"),
}


def _find_header_row(ws, anchor: str = "Индикаторы") -> int:
    """Найти строку шапки листа по известному заголовку в любой колонке."""
    for i in range(1, ws.max_row + 1):
        for c in ws[i]:
            if c.value == anchor:
                return i
    raise ValueError(f"строка шапки с заголовком «{anchor}» не найдена")


def _col(ws, header_row: int, name: str) -> int:
    """Номер колонки по тексту её заголовка в строке шапки."""
    for c in ws[header_row]:
        if c.value == name:
            return c.column
    raise ValueError(f"колонка «{name}» не найдена в строке {header_row}")


def _text(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "" if value is None else str(value).strip()


def read_remarks(path: Path) -> list[Remark]:
    """Все строки трёх листов, где эксперт что-то заполнил, — по порядку листов и строк."""
    wb = load_workbook(path)
    remarks = []
    for sheet, (anchor, key_col, title_col) in _SHEETS.items():
        if sheet not in wb.sheetnames:
            raise ValueError(f"в файле нет листа «{sheet}» — это не книга результата прогона")
        ws = wb[sheet]
        hdr = _find_header_row(ws, anchor)
        cols = {c.value: c.column for c in ws[hdr] if c.value}
        for r in range(hdr + 1, ws.max_row + 1):
            get = lambda name: _text(ws.cell(r, cols[name]).value) if name in cols else ""   # noqa: E731
            agree, correct, score, text = get(_AGREE), get(_CORRECT), get(_SCORE), get(_TEXT)
            if not (agree or correct or score or text):
                continue
            key, title = get(key_col), get(title_col)
            if sheet == _SHEET_VERDICTS:   # «1. межведомственное взаимодействие»
                m = re.match(r"(\d+)\.\s*(.*)", key, re.S)
                key, title = (m.group(1), m.group(2)) if m else (key, key)
            if len(title) > _TITLE_LEN:
                title = title[:_TITLE_LEN].rstrip() + "…"
            remarks.append(Remark(sheet=sheet, key=key, title=title, agree=agree or None, correct=correct or None,
                                  score=int(score) if score.isdigit() else None, text=text))
    return remarks


def is_queued(r: Remark) -> bool:
    """В очередь диалога: «Согласны? = нет» или заполненное «Замечание»."""
    return r.agree == "нет" or bool(r.text)


def summary(remarks: list[Remark]) -> str:
    """Сводка того, что не идёт в очередь: согласия с вердиктами и оценки без текста."""
    quiet = [r for r in remarks if not is_queued(r)]
    parts = []
    agreed = sum(1 for r in quiet if r.sheet == _SHEET_VERDICTS and r.agree == "да")
    if agreed:
        parts.append(f"вердикты — согласен с {agreed}")
    for sheet, noun in ((_SHEET_MAP, "строк карты"), (_SHEET_REQUEST, "позиций запроса")):
        # строки без номера (заголовок группы в запросе) — не позиции, в счёт не идут
        scores = [r.score for r in quiet if r.sheet == sheet and r.key and r.score is not None]
        if scores:
            parts.append(f"{noun}: {len(scores)}, средняя оценка {sum(scores) / len(scores):.1f}".replace(".", ","))
    return ("Без замечаний: " + "; ".join(parts) + ".") if parts else ""


def remark_message(r: Remark) -> str:
    """Замечание — сообщением пользователя в диалог."""
    where = {
        _SHEET_VERDICTS: f"лист Вердикты, индикатор {r.key} «{r.title}»",
        _SHEET_MAP: f"Карта гипотез, строка {r.key} («{r.title}»)",
        _SHEET_REQUEST: f"Запрос показателей, позиция {r.key} («{r.title}»)",
    }[r.sheet]
    if not r.key:   # строка без номера: замечание под таблицей или у заголовка группы
        where = f"общее замечание к листу {r.sheet}" + (f" (у строки «{r.title}»)" if r.title else "")
    parts = [f"Замечание эксперта из его файла — {where}."]
    if r.agree:
        parts.append(f"С вердиктом согласен: {r.agree}.")
    if r.correct:
        parts.append(f"Верный вердикт, по мнению эксперта: {r.correct}.")
    if r.score is not None:
        parts.append(f"Оценка: {r.score} из 5.")
    if r.text:
        parts.append(f"Замечание: {r.text}")
    return " ".join(parts)
