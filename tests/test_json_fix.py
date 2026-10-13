"""
Тесты app/json_fix.py — починка битого JSON ответа модели по схеме контракта (13.10.2026).

Случаи — те, что пойманы на паспорте: пропущенная скобка раздела в середине ответа и в конце;
плюс кавычки внутри текста строки.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from app.json_fix import repair_json


class _Inner(BaseModel):
    state: str
    items: list[str]


class _Group(BaseModel):
    first: _Inner
    second: _Inner


class _Root(BaseModel):
    title: str
    group: _Group
    tail: _Inner


SCHEMA = _Root.model_json_schema()
GOOD = {"title": "акт", "group": {"first": {"state": "есть", "items": ["п. 1"]},
                                  "second": {"state": "нет", "items": []}},
        "tail": {"state": "есть", "items": ["п. 2", "п. 3"]}}


def test_valid_json_unchanged():
    text = json.dumps(GOOD, ensure_ascii=False)
    assert repair_json(text, SCHEMA) == text


def test_missing_brace_before_next_section():
    """Раздел group не закрыт перед tail (как allocation перед control в паспорте) — скобка встаёт перед запятой."""
    text = json.dumps(GOOD, ensure_ascii=False).replace('[]}}, "tail"', '[]}, "tail"')
    assert json.loads(repair_json(text, SCHEMA)) == GOOD


def test_missing_braces_at_end():
    text = json.dumps(GOOD, ensure_ascii=False)[:-2]
    assert json.loads(repair_json(text, SCHEMA)) == GOOD


def test_inner_quotes_are_escaped():
    data = dict(GOOD, title='Порядок "Б" для органа А')
    text = json.dumps(data, ensure_ascii=False).replace('\\"', '"')
    assert json.loads(repair_json(text, SCHEMA)) == data


def test_quote_before_comma_inside_text_is_escaped():
    """Кавычка перед запятой внутри текста — не конец строки: после запятой не идёт ключ или значение."""
    data = dict(GOOD, title='название "Б", и далее')
    text = json.dumps(data, ensure_ascii=False).replace('\\"', '"')
    assert json.loads(repair_json(text, SCHEMA)) == data
