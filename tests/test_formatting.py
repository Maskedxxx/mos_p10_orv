"""Тесты форматирования для экранов и строк чата (app/formatting.py; 27.10 перенесены из tests/test_web.py)."""

from __future__ import annotations

from app import formatting


# ---- форматирование, которое видит аналитик ----


def test_step_duration_is_shown_in_seconds():
    assert formatting.format_seconds(0.5) == "0,5 сек"
    assert formatting.format_seconds(139.4) == "139 сек"


def test_elapsed_time_is_shown_in_minutes_and_seconds():
    assert formatting.format_elapsed(42) == "42 сек"
    assert formatting.format_elapsed(252) == "4 мин 12 сек"


def test_russian_plural_forms():
    assert formatting.plural(1, "гипотеза", "гипотезы", "гипотез") == "гипотеза"
    assert formatting.plural(3, "гипотеза", "гипотезы", "гипотез") == "гипотезы"
    assert formatting.plural(15, "гипотеза", "гипотезы", "гипотез") == "гипотез"
    assert formatting.plural(31, "показатель", "показателя", "показателей") == "показатель"
