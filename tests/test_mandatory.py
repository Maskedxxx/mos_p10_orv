"""
Тесты обязательных показателей бланка по типу акта (2.7, итерация 4, доработка АМ).

Набор сгенерирован из «Формы для ИИ 2.0» В. Д. Чуракова: тест держит число показателей
по типам, единицы закрытого перечня и то, что каждый показатель проходит контракт позиции.
"""

from __future__ import annotations

import pytest

from app.contracts import RequestItem
from app.mandatory_indicators import ACT_TYPES, MANDATORY, NO_TYPE, mandatory_for


def test_counts_match_the_form():
    """Сверено с формой 12.10: КНД 17, господдержка 20, госуслуга 11 (№ 1–3, 9–16), руководство 17, ОТ 19."""
    counts = {k: len(v) for k, v in MANDATORY.items()}
    assert counts == {"контрольно-надзорная деятельность": 17, "господдержка": 20, "предоставление госуслуги": 11,
                      "руководство по соблюдению обязательных требований": 17, "обязательные требования": 19}
    assert [i.code for i in MANDATORY["предоставление госуслуги"]] == [
        "ГУ-1", "ГУ-2", "ГУ-3", "ГУ-9", "ГУ-10", "ГУ-11", "ГУ-12", "ГУ-13", "ГУ-14", "ГУ-15", "ГУ-16"]
    assert ACT_TYPES[-1] == NO_TYPE


def test_units_translated_and_verbatim_text_kept():
    gp18 = next(i for i in MANDATORY["господдержка"] if i.code == "ГП-18")
    assert gp18.unit == "листов" and gp18.text.endswith("(л.)")       # «л.» → «листов»
    knd13 = next(i for i in MANDATORY["контрольно-надзорная деятельность"] if i.code == "КНД-13")
    assert knd13.unit == "часов"                                        # «час.» → «часов»
    gu15 = next(i for i in MANDATORY["предоставление госуслуги"] if i.code == "ГУ-15")
    assert gu15.unit == "чел."
    gp12 = next(i for i in MANDATORY["господдержка"] if i.code == "ГП-12")
    assert gp12.kind == "перечень или реестр" and gp12.unit is None


@pytest.mark.parametrize("act_type", [t for t in ACT_TYPES if t != NO_TYPE])
def test_every_mandatory_indicator_is_a_valid_request_item(act_type):
    """Код дописывает пропущенный показатель дословно (Р4) — он обязан проходить контракт позиции."""
    for i, ind in enumerate(MANDATORY[act_type], start=1):
        RequestItem(num=str(i), kind=ind.kind, metric=ind.metric, unit=ind.unit, mandatory=[ind.code])


def test_union_without_duplicates():
    """КНД и руководство совпадают по тексту — одна запись с обоими кодами."""
    merged = mandatory_for(["контрольно-надзорная деятельность", "руководство по соблюдению обязательных требований"])
    assert len(merged) == 17
    assert merged[0]["codes"] == ["КНД-1", "Р-1"]
    assert mandatory_for([NO_TYPE]) == [] and mandatory_for([]) == []
