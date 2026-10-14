"""
Тесты конфига 12 индикаторов и служебных настроек (app/config.py).
"""

from __future__ import annotations

from app.config import (
    ALLOWED_TOOLS_TEXT,
    ALLOWED_TOOLS_WEB,
    COMPARISON_REGIONS,
    PROMPTS_DIR,
    REQUEST_YEARS_DEPTH,
    all_indicators,
    get_indicator,
    prompt_path,
    text_indicators,
    web_indicators,
)

TEXT_INDICATOR_NUMBERS = {1, 2, 5, 6, 7, 8, 9}   # 7, 8, 9 — с 13.10 только текст акта
WEB_INDICATOR_NUMBERS = {3, 4, 10, 11, 12}


def test_exactly_12_indicators_without_gaps_or_duplicates():
    indicators = all_indicators()
    numbers = [ind.number for ind in indicators]

    assert len(indicators) == 12
    assert sorted(numbers) == list(range(1, 13))
    assert len(set(numbers)) == 12


def test_text_indicators_are_exactly_expected_set():
    numbers = {ind.number for ind in text_indicators()}
    assert numbers == TEXT_INDICATOR_NUMBERS


def test_web_indicators_are_exactly_expected_set():
    numbers = {ind.number for ind in web_indicators()}
    assert numbers == WEB_INDICATOR_NUMBERS


def test_text_indicators_have_empty_tools_list():
    assert ALLOWED_TOOLS_TEXT == []


def test_web_indicators_have_both_tools():
    assert ALLOWED_TOOLS_WEB == ["WebSearch", "WebFetch"]


def test_request_years_depth_is_3():
    assert REQUEST_YEARS_DEPTH == 3


def test_comparison_regions_count_is_7():
    assert len(COMPARISON_REGIONS) == 7


def test_get_indicator_returns_expected_indicator():
    indicator = get_indicator(1)
    assert indicator.number == 1
    assert indicator.name == "межведомственное взаимодействие"


def test_prompt_path_returns_path_inside_prompts_dir_for_every_indicator():
    for n in range(1, 13):
        path = prompt_path(n)
        assert path.parent == PROMPTS_DIR
        assert path.name == f"02_indicator_{n:02d}.md"
