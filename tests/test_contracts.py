"""
Тесты контрактов app/contracts.py: запрет лишних полей, enum-значения,
согласованность missing_data, непустой data у гипотезы, непустые строки,
JSON-схема (additionalProperties: false) и латиница всех имён полей.
"""

from __future__ import annotations

import re
import typing

import pytest
from pydantic import BaseModel, ValidationError

from app.contracts import (
    Buckets,
    DataItem,
    DataRequest,
    Effect,
    Hypothesis,
    HypothesisMap,
    HypothesisSet,
    MapRow,
    Merge,
    RequestItem,
    RunMeta,
    Verdict,
)

ALL_MODELS = [
    Verdict,
    Buckets,
    Effect,
    DataItem,
    Hypothesis,
    HypothesisSet,
    MapRow,
    Merge,
    HypothesisMap,
    RequestItem,
    DataRequest,
    RunMeta,
]


# ---- extra="forbid": лишнее поле отвергается — по одному тесту на модель ----


def test_verdict_extra_field_rejected(valid_verdict_ok):
    with pytest.raises(ValidationError):
        Verdict(**valid_verdict_ok, extra_field="лишнее")


def test_buckets_extra_field_rejected():
    with pytest.raises(ValidationError):
        Buckets(extra_field="лишнее")


def test_effect_extra_field_rejected(valid_effect):
    with pytest.raises(ValidationError):
        Effect(**valid_effect, extra_field="лишнее")


def test_data_item_extra_field_rejected(valid_data_item):
    with pytest.raises(ValidationError):
        DataItem(**valid_data_item, extra_field="лишнее")


def test_hypothesis_extra_field_rejected(valid_hypothesis):
    with pytest.raises(ValidationError):
        Hypothesis(**valid_hypothesis, extra_field="лишнее")


def test_hypothesis_set_extra_field_rejected(valid_hypothesis_set):
    with pytest.raises(ValidationError):
        HypothesisSet(**valid_hypothesis_set, extra_field="лишнее")


def test_map_row_extra_field_rejected(valid_map_row):
    with pytest.raises(ValidationError):
        MapRow(**valid_map_row, extra_field="лишнее")


def test_merge_extra_field_rejected(valid_merge):
    with pytest.raises(ValidationError):
        Merge(**valid_merge, extra_field="лишнее")


def test_hypothesis_map_extra_field_rejected():
    with pytest.raises(ValidationError):
        HypothesisMap(extra_field="лишнее")


def test_request_item_extra_field_rejected(valid_request_item):
    with pytest.raises(ValidationError):
        RequestItem(**valid_request_item, extra_field="лишнее")


def test_data_request_extra_field_rejected():
    with pytest.raises(ValidationError):
        DataRequest(extra_field="лишнее")


def test_run_meta_extra_field_rejected(valid_run_meta):
    with pytest.raises(ValidationError):
        RunMeta(**valid_run_meta, extra_field="лишнее")


# ---- неверное значение enum отвергается ----


def test_verdict_invalid_enum_value_rejected(valid_verdict_ok):
    with pytest.raises(ValidationError):
        Verdict(**{**valid_verdict_ok, "verdict": "не тот статус"})


def test_data_item_invalid_source_rejected(valid_data_item):
    with pytest.raises(ValidationError):
        DataItem(**{**valid_data_item, "source": "неизвестный источник"})


# ---- согласованность missing_data ----


def test_verdict_missing_data_forbidden_when_not_no_data(valid_verdict_ok):
    """missing_data при вердикте не "нет данных" отвергается."""
    with pytest.raises(ValidationError):
        Verdict(**{**valid_verdict_ok, "missing_data": "что-то"})


def test_verdict_no_data_requires_missing_data(valid_verdict_no_data):
    """"нет данных" без missing_data отвергается."""
    data = dict(valid_verdict_no_data)
    data.pop("missing_data")
    with pytest.raises(ValidationError):
        Verdict(**data)


# ---- Hypothesis.data не может быть пустым ----


def test_hypothesis_empty_data_rejected(valid_hypothesis):
    with pytest.raises(ValidationError):
        Hypothesis(**{**valid_hypothesis, "data": []})


# ---- Verdict.attempts: сколько попыток вызова понадобилось ----


def test_verdict_attempts_defaults_to_1(valid_verdict_ok):
    verdict = Verdict(**valid_verdict_ok)
    assert verdict.attempts == 1


def test_verdict_attempts_below_1_rejected(valid_verdict_ok):
    with pytest.raises(ValidationError):
        Verdict(**{**valid_verdict_ok, "attempts": 0})


# ---- пустая/пробельная строка в обязательном текстовом поле отвергается ----


@pytest.mark.parametrize(
    "model_cls, base_fixture_name, field, blank_value",
    [
        (Verdict, "valid_verdict_ok", "name", ""),
        (Verdict, "valid_verdict_ok", "rationale", "   "),
        (Effect, "valid_effect", "metric", ""),
        (Effect, "valid_effect", "how_to_calculate", "   "),
        (DataItem, "valid_data_item", "metric", "  "),
        (Hypothesis, "valid_hypothesis", "hypothesis", ""),
        (Hypothesis, "valid_hypothesis", "affected", "   "),
        (HypothesisSet, "valid_hypothesis_set", "name", ""),
        (HypothesisSet, "valid_hypothesis_set", "why_this_many", "  "),
        (MapRow, "valid_map_row", "hypothesis", "   "),
        (Merge, "valid_merge", "reason", ""),
        (RequestItem, "valid_request_item", "num", ""),
        (RequestItem, "valid_request_item", "metric", "  "),
        (RunMeta, "valid_run_meta", "run_id", ""),
        (RunMeta, "valid_run_meta", "act_filename", "   "),
        (RunMeta, "valid_run_meta", "started_at", ""),
        (RunMeta, "valid_run_meta", "status", "  "),
    ],
)
def test_blank_required_string_field_rejected(request, model_cls, base_fixture_name, field, blank_value):
    base_data = request.getfixturevalue(base_fixture_name)
    data = {**base_data, field: blank_value}
    with pytest.raises(ValidationError):
        model_cls(**data)


# ---- JSON-схема: additionalProperties: false у каждой модели ----


@pytest.mark.parametrize("model_cls", ALL_MODELS)
def test_model_json_schema_has_additional_properties_false(model_cls):
    schema = model_cls.model_json_schema()
    assert schema.get("additionalProperties") is False


# ---- все имена полей во всех моделях — латиница (рекурсивный обход) ----

FIELD_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")

# Корневые модели-артефакты: обход по их полям должен рекурсивно достать все 12 моделей
ROOT_MODELS_FOR_FIELD_NAME_CHECK = [Verdict, Buckets, HypothesisSet, HypothesisMap, DataRequest, RunMeta]


def _extract_nested_models(annotation) -> list[type[BaseModel]]:
    """Извлечь вложенные pydantic-модели из аннотации поля (list[...], X | None и т.п.)."""
    found: list[type[BaseModel]] = []
    origin = typing.get_origin(annotation)
    if origin is not None:
        for arg in typing.get_args(annotation):
            found.extend(_extract_nested_models(arg))
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    return found


def _collect_models_recursively(model_cls, collected: set | None = None) -> set:
    """Рекурсивно обойти модель и все модели, вложенные через её поля."""
    if collected is None:
        collected = set()
    if model_cls in collected:
        return collected
    collected.add(model_cls)
    for field_info in model_cls.model_fields.values():
        for nested_cls in _extract_nested_models(field_info.annotation):
            _collect_models_recursively(nested_cls, collected)
    return collected


def test_all_field_names_across_all_models_are_latin():
    all_models: set = set()
    for root in ROOT_MODELS_FOR_FIELD_NAME_CHECK:
        all_models |= _collect_models_recursively(root)

    # Защита от того, что рекурсия случайно не дошла до части моделей
    # (13: + PairDecision — решение по кандидату на слияние, узел [5], ОС 2026-09-04;
    #  14: + SourceRef — происхождение позиции запроса, узел [6], ОС 2026-09-04)
    assert len(all_models) == 14

    for model_cls in all_models:
        for field_name in model_cls.model_fields:
            assert FIELD_NAME_PATTERN.match(field_name), (
                f"{model_cls.__name__}.{field_name} не проходит regex имён полей"
            )
