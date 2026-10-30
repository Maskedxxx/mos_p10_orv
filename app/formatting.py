"""
Форматирование чисел, дат и текста для экранов и строк чата (27.10, модульность: вынесено из app/web.py —
этими функциями пользуются и страницы, и действия чата app/dialog_jobs.py).
"""

from __future__ import annotations

from datetime import datetime


def format_cost(value: float | None, known: bool = True, *, digits: int = 2) -> str:
    """Неизвестная стоимость не отображается как бесплатный вызов."""
    return f"${value:.{digits}f}" if known and value is not None else "не определена"


def plural(number: int, one: str, few: str, many: str) -> str:
    """Русское склонение при числе: 1 гипотеза, 2 гипотезы, 5 гипотез."""
    if 11 <= number % 100 <= 14:
        return many
    last_digit = number % 10
    if last_digit == 1:
        return one
    if 2 <= last_digit <= 4:
        return few
    return many


def format_seconds(seconds: float) -> str:
    """Длительность шага: «0,5 сек» для коротких, «139 сек» для длинных."""
    if seconds < 10:
        return f"{seconds:.1f} сек".replace(".", ",")
    return f"{round(seconds)} сек"


def format_elapsed(seconds: float) -> str:
    """Время с начала прогона: «42 сек», «4 мин 12 сек»."""
    total = int(round(seconds))
    if total < 60:
        return f"{total} сек"
    return f"{total // 60} мин {total % 60:02d} сек"


def format_moment(moment: datetime | None) -> str:
    """Дата и время прогона для экрана истории: «25.08.2026 08:13»."""
    return moment.strftime("%d.%m.%Y %H:%M") if moment is not None else "—"


def parse_moment(value: str | None) -> datetime | None:
    """Разобрать отметку времени из progress.json; мусор — это None, а не исключение."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def shorten(text: str, limit: int = 80) -> str:
    """Обрезать длинный текст (причину сбоя) до одной строки графа."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
