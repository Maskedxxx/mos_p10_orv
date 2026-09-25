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
    ActPassport,
    Buckets,
    DataItem,
    DataRequest,
    DialogTurn,
    Effect,
    Hypothesis,
    HypothesisMap,
    HypothesisSet,
    MapRow,
    Merge,
    MergeOutput,
    PairDecision,
    RequestItem,
    RequestOutput,
    RunMeta,
    SourceRef,
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
    PairDecision,
    HypothesisMap,
    SourceRef,
    RequestItem,
    DataRequest,
    ActPassport,
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


def test_effect_requires_expected_trend(valid_effect):
    """Пул 3, п. 1б (письмо заказчика 2026-09-04): у эффекта обязана быть ожидаемая динамика."""
    without = {k: v for k, v in valid_effect.items() if k != "expected_trend"}
    with pytest.raises(ValidationError):
        Effect(**without)


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
        (Effect, "valid_effect", "expected_trend", ""),
        (DataItem, "valid_data_item", "metric", "  "),
        (Hypothesis, "valid_hypothesis", "hypothesis", ""),
        (Hypothesis, "valid_hypothesis", "grounds", "   "),
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
ROOT_MODELS_FOR_FIELD_NAME_CHECK = [Verdict, Buckets, HypothesisSet, HypothesisMap, DataRequest, ActPassport, RunMeta, DialogTurn]


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
    assert len(all_models) == 51   # v2: + ActPassport с 24 вложенными, + PairDecision, + SourceRef;
    # итерация 3: + DialogTurn, 10 операций диалога и SplitPart

    for model_cls in all_models:
        for field_name in model_cls.model_fields:
            assert FIELD_NAME_PATTERN.match(field_name), (
                f"{model_cls.__name__}.{field_name} не проходит regex имён полей"
            )


# ---- v2 (2026-09-04): описание у каждого поля контрактов structured output ----

# Контракты, которые уходят модели как --json-schema: описание поля = мини-промпт к нему.
# Verdict сюда не входит намеренно: его поля описаны в промптах индикаторов текстом.
OUTPUT_CONTRACT_ROOTS = [HypothesisSet, HypothesisMap, DataRequest, ActPassport, DialogTurn]


def test_every_output_contract_field_has_description():
    """Требование владельца: у КАЖДОГО поля контракта structured output непустое
    description — pydantic кладёт его в JSON-схему, модель читает как инструкцию."""
    models: set = set()
    for root in OUTPUT_CONTRACT_ROOTS:
        models |= _collect_models_recursively(root)
    missing = [
        f"{m.__name__}.{name}"
        for m in models for name, info in m.model_fields.items()
        if not (info.description and info.description.strip())
    ]
    assert missing == [], f"поля без description: {missing}"


def test_passport_schema_fits_argv_limit():
    """Схема паспорта уходит одним аргументом argv; лимит Linux MAX_ARG_STRLEN = 131072."""
    import json
    size = len(json.dumps(ActPassport.model_json_schema(), ensure_ascii=False).encode())
    assert size < 100_000, size


# ---- v2: валидаторы новых контрактов ловят нарушения (перенос ручных проверок) ----


def _hyp(**kw) -> dict:
    base = {"hypothesis": "h", "grounds": "g",
            "effects": [{"affected": "бизнес", "metric": "m", "how_to_calculate": "c", "expected_trend": "рост"}],
            "data": [{"metric": "d", "source": "запрос органу"}]}
    base.update(kw)
    return base


def test_helpers_build_valid_objects():
    """Страж: помощники собирают валидные объекты. Иначе тесты «отвергается» проходят
    по чужой причине — так было с v2.4 до 2026-09-25: эффект без обязательного
    expected_trend отвергался, и шесть тестов ничего не проверяли."""
    Hypothesis(**_hyp())
    HypothesisMap(rows=[_row(1, [1])])


def test_hypothesis_two_effects_same_group_rejected():
    with pytest.raises(ValidationError):
        Hypothesis(**_hyp(effects=[
            {"affected": "бизнес", "metric": "a", "how_to_calculate": "c", "expected_trend": "рост"},
            {"affected": "бизнес", "metric": "b", "how_to_calculate": "c", "expected_trend": "рост"},
        ]))


def test_data_item_open_requires_where_and_request_forbids_it():
    with pytest.raises(ValidationError):
        DataItem(metric="x", source="открытый")
    with pytest.raises(ValidationError):
        DataItem(metric="x", source="запрос органу", where="сайт")


def _row(i: int, ind: list[int]) -> dict:
    return {"row_id": i, "indicators": ind, "hypothesis": "h", "grounds": "g",
            "effects": [{"affected": "бизнес", "metric": "m", "how_to_calculate": "c", "expected_trend": "рост"}],
            "data": [{"metric": "d", "source": "запрос органу"}]}


@pytest.mark.parametrize("payload", [
    {"rows": [_row(2, [1]), _row(1, [2])]},                                                  # row_id не по возрастанию
    {"rows": [_row(1, [1]), _row(1, [2])]},                                                  # row_id повторяется
    {"rows": [_row(1, [1])], "decisions": [{"row_ids": [1, 2], "merge": True, "reason": "r"}]},  # «слить» без слияния
    {"rows": [_row(1, [1, 2])], "merges": [{"merged_row_ids": [1, 2], "reason": "r"}],
     "decisions": [{"row_ids": [1, 2], "merge": False, "reason": "r"}]},                    # слито при «не сливать»
    {"rows": [_row(1, [1])], "merges": [{"merged_row_ids": [1, 2], "reason": "r"},
                                       {"merged_row_ids": [2, 3], "reason": "r"}]},          # строка в двух слияниях
    {"rows": [_row(1, [7, 1])]},                                                             # indicators не по порядку
])
def test_hypothesis_map_inconsistency_rejected(payload):
    with pytest.raises(ValidationError):
        HypothesisMap(**payload)


def _item(**kw) -> dict:
    base = {"num": "1", "kind": "показатель по годам", "metric": "количество заявок (п. 1)", "unit": "ед.",
            "for_rows": [1], "sources": [{"row_id": 1, "metric": "m"}]}
    base.update(kw)
    return base


@pytest.mark.parametrize("item", [
    _item(metric="количество и доля уведомлений с отказом"),                  # две величины
    _item(metric="сумма грантов и количество получателей"),
    _item(unit=None),                                                        # показатель без единицы
    _item(kind="перечень или реестр", unit="ед."),                          # перечень с единицей
    _item(for_rows=[2]),                                                     # for_rows ≠ sources
    _item(breakdown=["по основаниям отказа, предусмотренным пунктом 5.3"]),  # разрез без перечисления
    _item(unit="%"),                                                         # процентов нет
])
def test_request_item_violations_rejected(item):
    with pytest.raises(ValidationError):
        RequestItem(**item)


# Нумерация без «подряд» (план итерации 3, шаг 1.3): после удаления строки карты
# или позиции запроса в версии диалога остаётся дырка — номер закреплён за строкой.
# Строгая нумерация «с 1 подряд» — только в ответе модели (MergeOutput, RequestOutput):
# там её ловит runner.call и просит модель исправиться через --resume.
# ----------------------------------------------------------------------


def test_hypothesis_map_accepts_gaps_merge_output_does_not():
    """Карта с номерами 1, 3 — законная версия после удаления строки; от модели так нельзя."""
    payload = {"rows": [_row(1, [1]), _row(3, [2])]}
    assert [r.row_id for r in HypothesisMap(**payload).rows] == [1, 3]
    with pytest.raises(ValidationError):
        MergeOutput(**payload)


def test_request_accepts_gaps_and_order_request_output_does_not():
    """Бланк с номерами 1, 3 и перечнем раньше счётчика — законная версия; от модели так нельзя."""
    items = [
        _item(kind="перечень или реестр", unit=None, metric="перечень"),
        _item(num="3", metric="количество", sources=[{"row_id": 1, "metric": "m2"}]),
    ]
    assert [it.num for it in DataRequest(items=items, years=[2025]).items] == ["1", "3"]
    with pytest.raises(ValidationError):
        RequestOutput(items=items, years=[2025])


def test_request_num_duplicates_rejected():
    with pytest.raises(ValidationError):
        DataRequest(items=[_item(), _item(metric="другое", sources=[{"row_id": 1, "metric": "m2"}])], years=[2025])


def test_strict_output_schemas_identical_to_base():
    """Модель видит ту же схему, что и раньше: строгость только в проверке, не в промпте."""
    assert MergeOutput.model_json_schema() == HypothesisMap.model_json_schema()
    assert RequestOutput.model_json_schema() == DataRequest.model_json_schema()


def test_request_lists_must_follow_counters():
    with pytest.raises(ValidationError):
        RequestOutput(items=[
            _item(kind="перечень или реестр", unit=None, metric="перечень"),
            _item(num="2", metric="количество", sources=[{"row_id": 1, "metric": "m2"}]),
        ], years=[2025])


def test_passport_state_must_match_content(valid_passport):
    """«есть» без перечня и «нет» с перечнем отвергаются; проект с номером — тоже."""
    bad = {**valid_passport, "requirements": {**valid_passport["requirements"], "state": "есть"}}
    with pytest.raises(ValidationError):
        ActPassport(**bad)
    bad = {**valid_passport, "status": {**valid_passport["status"], "status": "проект"}}
    with pytest.raises(ValidationError):
        ActPassport(**bad)   # у проекта не может быть номера и даты


def test_passport_valid_minimal(valid_passport):
    assert ActPassport(**valid_passport).status.status == "принят"
