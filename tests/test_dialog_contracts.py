"""
Тесты контрактов диалогового режима (план итерации 3): ответ оркестратора
DialogTurn — текст пользователю и план из закрытого списка операций; служебные
Remark (замечание эксперта из файла) и VersionState (состояние версий прогона).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.contracts import (
    AddMapRow, AppendRequest, BuildHypotheses, DeleteMapRow, DeleteRequestItem, DialogTurn, EditText,
    MergeMapRows, RecheckIndicator, Remark, SetVerdict, SplitRequestItem, VersionState,
)

_EFFECT = {"affected": "бизнес", "metric": "издержки, руб.", "how_to_calculate": "сумма", "expected_trend": "рост"}


def _turn(*plan) -> DialogTurn:
    return DialogTurn.model_validate({"reply": "Готово.", "plan": list(plan)})


def test_plan_of_every_kind_parses_to_its_class():
    turn = _turn(
        {"kind": "правка текста", "sheet": "строка карты", "key": "3", "field": "гипотеза", "new_text": "короче", "reason": "вода"},
        {"kind": "смена вердикта", "indicator": 7, "verdict": "нарушен", "rationale": "15 форм", "reason": "эксперт"},
        {"kind": "удалить строку карты", "row_id": 2, "reason": "эксперт: гипотеза несостоятельна"},
        {"kind": "добавить строку карты", "indicators": [10], "hypothesis": "h", "grounds": "g",
         "effects": [_EFFECT], "data": [{"metric": "число вольеров", "source": "запрос органу"}], "reason": "эксперт"},
        {"kind": "слить строки карты", "row_ids": [3, 5], "hypothesis": "одна проблема", "reason": "дубль"},
        {"kind": "удалить позицию запроса", "num": "4", "reason": "орган не ведёт"},
        {"kind": "разделить позицию запроса", "num": "2", "parts": [{"metric": "сумма (п. 3)", "unit": "тыс. руб."},
                                                                    {"metric": "количество (п. 3)", "unit": "ед."}], "reason": "две величины"},
        {"kind": "перепроверить индикатор", "indicator": 3, "clarification": "проверь приказы Минтранса", "reason": "эксперт"},
        {"kind": "построить гипотезы", "indicator": 7, "reason": "вердикт стал «нарушен»"},
        {"kind": "дополнить запрос", "row_ids": [9], "reason": "новая строка"},
    )
    assert [type(op) for op in turn.plan] == [
        EditText, SetVerdict, DeleteMapRow, AddMapRow, MergeMapRows, DeleteRequestItem,
        SplitRequestItem, RecheckIndicator, BuildHypotheses, AppendRequest,
    ]


def test_question_has_empty_plan():
    assert DialogTurn.model_validate({"reply": "Индикатор 7 не применим, потому что…"}).plan == []


@pytest.mark.parametrize("op", [
    {"kind": "удалить всё", "reason": "x"},                                                        # неизвестная операция
    {"kind": "смена вердикта", "indicator": 7, "verdict": "ошибка", "rationale": "r", "reason": "x"},  # «ошибка» ставит сервис
    {"kind": "смена вердикта", "indicator": 7, "verdict": "нет данных", "rationale": "r", "reason": "x"},  # без missing_data
    {"kind": "смена вердикта", "indicator": 13, "verdict": "нарушен", "rationale": "r", "reason": "x"},
    {"kind": "слить строки карты", "row_ids": [3], "hypothesis": "h", "reason": "x"},              # сливать нечего
    {"kind": "разделить позицию запроса", "num": "2", "parts": [{"metric": "m", "unit": "ед."}], "reason": "x"},
    {"kind": "разделить позицию запроса", "num": "2", "parts": [{"metric": "m", "unit": "суток"},
                                                                {"metric": "k", "unit": "ед."}], "reason": "x"},  # «суток» убраны (2.7)
    {"kind": "правка текста", "sheet": "вердикт", "key": "3", "field": "гипотеза", "new_text": "t", "reason": "x"},  # поле не того листа
    {"kind": "правка текста", "sheet": "строка карты", "key": "3", "field": "как считать", "new_text": "t", "reason": "x"},  # эффект без номера
    {"kind": "перепроверить индикатор", "indicator": 3, "clarification": " ", "reason": "x"},     # пустое уточнение
])
def test_invalid_operations_rejected(op):
    with pytest.raises(ValidationError):
        _turn(op)


def test_dialog_turn_schema_has_no_discriminator_keyword():
    """CLI отвергает ключевое слово discriminator в --json-schema (проверено 2026-09-25, шаг 0)."""
    assert "discriminator" not in json.dumps(DialogTurn.model_json_schema(), ensure_ascii=False)


def test_remark_and_version_state_defaults():
    r = Remark(sheet="Вердикты", key="7", title="7. отсутствие избыточных процедур", agree="нет",
               correct="не соблюдён", text="15 форм избыточны")
    assert r.score is None
    s = VersionState(next_row_id=5, next_item_num=8)
    assert s.current == 0 and s.unverified_ids == {} and s.marks.edited_rows == []
