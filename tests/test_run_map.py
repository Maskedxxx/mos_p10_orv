"""Тесты app/run_map.py — карта прогона для помощника (итерация 4, 14.10)."""

from __future__ import annotations

import pytest

from app.run_map import ARTIFACTS, EXCEL_MAP, build_run_map, describe_model, resolve_field


def test_every_contract_field_is_described_fully():
    """Каждое поле каждого контракта файлов прогона — в карте, с описанием полностью (как в Field(description))."""
    text = build_run_map()
    for art in ARTIFACTS:
        if art.contract is None:
            continue
        for name, info in art.contract.model_fields.items():
            assert f"`{name}`" in text, (art.path, name)
            if info.description:
                assert " ".join(info.description.split()) in text, (art.path, name)


def test_indicator_tables_described():
    """Таблицы проверки всех индикаторов со своим контрактом — в карте (поля сверх вердикта)."""
    from app.nodes.verdicts import INDICATOR_OUTPUT
    text = build_run_map()
    for n in INDICATOR_OUTPUT:
        assert f"`{n:02d}.checks.json`" in text, n
    assert "`references`" in text and "`to_request`" in text and "`refusals`" in text


@pytest.mark.parametrize("col", EXCEL_MAP, ids=lambda c: f"{c.sheet}:{c.column}")
def test_excel_map_fields_exist(col):
    """Таблица «Excel ← поле» не ссылается на несуществующие поля."""
    contract = next(a.contract for a in ARTIFACTS if a.path == col.file)
    for path in col.fields:
        assert resolve_field(contract, path), (col.file, path)


def test_nested_models_described_once():
    from app.contracts import HypothesisMap
    lines = describe_model(HypothesisMap)
    assert sum(1 for line in lines if line.strip().startswith("`MapRow`")) == 1
