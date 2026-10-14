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
from app.contracts import HypothesisSetOutput, MergeOutput, VerdictOutput  # noqa: E402

ROOT_MODELS_FOR_FIELD_NAME_CHECK = [Verdict, Buckets, HypothesisSet, HypothesisMap, DataRequest, ActPassport, RunMeta, DialogTurn,
                                    VerdictOutput, HypothesisSetOutput, MergeOutput]


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
    assert len(all_models) == 62   # v2: + ActPassport с 24 вложенными, + PairDecision, + SourceRef;
    # итерация 3: + DialogTurn, 10 операций диалога и SplitPart;
    # 2.5: + строгие ответы с выжимкой — VerdictOutput, HypothesisSetOutput, HypothesisOutput,
    # EffectOutput, MergeOutput, MapRowOutput;
    # 2.7: + DataItemOutput — показатель в ответе модели без пометки эксперта (Ж3), + MarkNotTracked, + Developer (орган-разработчик, А)
    # 14.10: + NumberItemOutput и OrganQuestion — числа и вопросы органу в ответе гипотез разными списками (ошибка 4)

    for model_cls in all_models:
        for field_name in model_cls.model_fields:
            assert FIELD_NAME_PATTERN.match(field_name), (
                f"{model_cls.__name__}.{field_name} не проходит regex имён полей"
            )


# ---- v2 (2026-09-04): описание у каждого поля контрактов structured output ----

# Контракты, которые уходят модели как --json-schema: описание поля = мини-промпт к нему.
# 2.5: Verdict вошёл — у полей вердикта теперь описания (правило владельца «description = промпт к полю»).
OUTPUT_CONTRACT_ROOTS = [HypothesisSet, HypothesisMap, DataRequest, ActPassport, DialogTurn, Verdict]


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
    _item(unit="суток"),                                                     # «суток» убраны (2.7, АИ)
    _item(kind="открытый вопрос", metric="В каких случаях?", unit="ед."),   # у вопроса нет единицы
    _item(kind="открытый вопрос", metric="В каких случаях?", unit=None, clause=None),  # вопрос без пункта
    _item(kind="открытый вопрос", metric="В каких случаях?", unit=None, clause="п. 1", breakdown=["а: б"]),  # и без разрезов
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


def test_strict_output_schemas_differ_from_base_only_by_briefs():
    """Запрос — схема побайтно прежняя (строгость только в проверке). Карта — прежние поля
    плюс обязательные выжимки для эксперта (2.5): модель должна видеть их обязательными."""
    assert RequestOutput.model_json_schema() == DataRequest.model_json_schema()

    def _def(schema, key):   # определение вложенной модели по характерному полю
        return next(d for d in schema["$defs"].values() if key in d.get("properties", {}))

    base, strict = HypothesisMap.model_json_schema(), MergeOutput.model_json_schema()
    for key, briefs in (("row_id", {"hypothesis_brief", "grounds_brief"}),
                        ("how_to_calculate", {"how_to_calculate_brief", "expected_trend_brief"})):
        b, s = _def(base, key), _def(strict, key)
        assert set(b["properties"]) == set(s["properties"])
        assert set(s["required"]) - set(b.get("required", [])) == briefs


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



# ---- 2.5: выжимка для эксперта — отдельный ключ рядом с полным текстом ----
# Хранимые контракты: выжимка необязательна (прогоны до 2.5 открываются).
# Ответ модели — строгий подкласс: выжимка обязательна, 1–4 пункта, пункт ≤ 250 знаков.

from app.contracts import (  # noqa: E402
    EffectOutput, HypothesisOutput, HypothesisSetOutput, MapRowOutput, MergeOutput, VerdictOutput,
)

_B = ["пункт с фактом (п. 2.1)"]
_EFFECT_BRIEFS = {"how_to_calculate_brief": _B, "expected_trend_brief": _B}


def _effect_out(**kw) -> dict:
    return {"affected": "бизнес", "metric": "m", "how_to_calculate": "c", "expected_trend": "рост", **_EFFECT_BRIEFS, **kw}


def _hyp_out(**kw) -> dict:
    return {**_hyp(effects=[_effect_out()]), "hypothesis_brief": _B, "grounds_brief": _B, **kw}


def _row_out(i: int, **kw) -> dict:
    return {**_row(i, [1]), "effects": [_effect_out()], "hypothesis_brief": _B, "grounds_brief": _B, **kw}


def _verdict_out(**kw) -> dict:
    return {"indicator": 1, "name": "межведомственное взаимодействие", "verdict": "соблюдён",
            "rationale": "полный разбор", "rationale_brief": _B, **kw}


def test_stored_contracts_accept_artifacts_without_brief():
    """Прогоны до 2.5 без выжимки открываются: в хранимых контрактах она необязательна."""
    assert Verdict(indicator=1, name="н", verdict="соблюдён", rationale="р").rationale_brief is None
    h = Hypothesis(**_hyp())
    assert h.hypothesis_brief is None and h.grounds_brief is None and h.effects[0].how_to_calculate_brief is None
    assert HypothesisMap(rows=[_row(1, [1])]).rows[0].hypothesis_brief is None


def test_output_helpers_build_valid_objects():
    """Страж, как test_helpers_build_valid_objects: иначе тесты «отвергается» ниже пройдут по чужой причине."""
    VerdictOutput(**_verdict_out())
    HypothesisOutput(**_hyp_out())
    HypothesisSetOutput(indicator=7, name="н", violation_kind="издержки", why_this_many="одно", hypotheses=[_hyp_out()])
    MergeOutput(rows=[_row_out(1)])


@pytest.mark.parametrize("build", [
    lambda: VerdictOutput(**{k: v for k, v in _verdict_out().items() if k != "rationale_brief"}),   # нет выжимки
    lambda: VerdictOutput(**_verdict_out(rationale_brief=[])),                                      # пустая
    lambda: VerdictOutput(**_verdict_out(rationale_brief=["а" * 251])),                             # пункт длиннее 250
    lambda: VerdictOutput(**_verdict_out(rationale_brief=["а"] * 5)),                               # больше 4 пунктов
    lambda: VerdictOutput(**_verdict_out(rationale_brief=["   "])),                                 # пустой пункт
    lambda: HypothesisOutput(**{k: v for k, v in _hyp_out().items() if k != "hypothesis_brief"}),
    lambda: HypothesisOutput(**_hyp_out(effects=[{**_effect_out(), "expected_trend_brief": []}])),
    lambda: HypothesisSetOutput(indicator=7, name="н", violation_kind="издержки", why_this_many="одно",
                                hypotheses=[{k: v for k, v in _hyp_out().items() if k != "grounds_brief"}]),
    lambda: MergeOutput(rows=[{k: v for k, v in _row_out(1).items() if k != "hypothesis_brief"}]),
    lambda: MergeOutput(rows=[_row_out(1, effects=[{k: v for k, v in _effect_out().items() if k != "how_to_calculate_brief"}])]),
], ids=["verdict-нет", "verdict-пусто", "verdict-длинно", "verdict-много", "verdict-пробелы", "гипотеза-нет",
        "эффект-пусто", "набор-нет-основания", "карта-нет", "карта-эффект-нет"])
def test_model_answer_requires_brief(build):
    with pytest.raises(ValidationError):
        build()


def test_output_schema_marks_briefs_required():
    """Модель видит выжимку обязательной — в том числе во вложенных эффектах."""
    assert "rationale_brief" in VerdictOutput.model_json_schema()["required"]
    defs = HypothesisSetOutput.model_json_schema()["$defs"]
    hyp = next(d for d in defs.values() if "hypothesis_brief" in d.get("properties", {}))
    eff = next(d for d in defs.values() if "how_to_calculate_brief" in d.get("properties", {}))
    assert {"hypothesis_brief", "grounds_brief"} <= set(hyp["required"])
    assert {"how_to_calculate_brief", "expected_trend_brief"} <= set(eff["required"])
    row = next(d for d in MergeOutput.model_json_schema()["$defs"].values() if "row_id" in d.get("properties", {}))
    assert {"hypothesis_brief", "grounds_brief"} <= set(row["required"])


# ---- 2.7 (итерация 4): гипотезы — Е, Г, Ж1, Ж2, Ж3 ----


@pytest.mark.parametrize("group", ["бизнес", "граждане", "город", "бизнес и граждане", "бизнес и город",
                                   "граждане и город", "бизнес, граждане и город"])
def test_affected_any_combination_of_groups(group):
    """Е: «кого задевает» — любое сочетание трёх групп; «бизнес и граждане» — прежним словом."""
    assert Effect(affected=group, metric="m", how_to_calculate="c", expected_trend="рост").affected == group


def test_effect_descriptions_iter4():
    """Г и Е: инструкции модели к полям эффекта."""
    how = Effect.model_fields["how_to_calculate"].description
    assert "прошивка (только если акт её требует)" in how and "бумага и печать" in how
    aff = Effect.model_fields["affected"].description
    assert "Сочетание ставь, когда одна и та же цифра касается сразу нескольких групп" in aff


def test_data_item_question_and_not_tracked_sources():
    """Ж2, Ж3: вопрос органу и «нигде не ведётся» — без where, как запрос органу."""
    assert DataItem(metric="В каких случаях привлекаются внештатные эксперты?", source="вопрос органу").where is None
    assert DataItem(metric="число погибших по видам нарушений", source="нигде не ведётся").where is None
    for src in ("вопрос органу", "нигде не ведётся"):
        with pytest.raises(ValidationError):
            DataItem(metric="x", source=src, where="сайт")


def test_data_item_source_description_allows_complaints_and_questions():
    """Ж1: жалобы и суды — у органа; Ж2: вопрос органу — с условиями."""
    d = DataItem.model_fields["source"].description
    assert "число жалоб и судебных споров по применению акта" in d
    assert "(их ищут в открытых источниках)" not in d
    assert "«вопрос органу»" in d


def test_model_cannot_answer_not_tracked():
    """Ж3: «нигде не ведётся» ставит только эксперт — в ответах моделей гипотез и слияния его нет."""
    from app.contracts import HypothesisSetOutput, MergeOutput
    bad = {"metric": "x", "source": "нигде не ведётся"}
    with pytest.raises(ValidationError):
        HypothesisSetOutput(indicator=1, name="н", violation_kind="издержки",
                            why_this_many="w", hypotheses=[_hyp_out(data=[bad])])
    with pytest.raises(ValidationError):
        MergeOutput(rows=[_row_out(1, data=[bad])])
    assert MergeOutput(rows=[_row_out(1, data=[{"metric": "вопрос?", "source": "вопрос органу"}])])
    # в хранимой строке карты значение допустимо — его ставит операция диалога
    assert MapRow(**{**_row(1, [1]), "data": [bad]})


# ---- 2.7 (итерация 4): запрос — АИ, АЛ, Ж2 ----


@pytest.mark.parametrize("unit", ["чел.", "листов", "%"])
def test_request_new_units(unit):
    """АИ: «чел.», «листов», «%» — допустимые единицы; процент — по исключению из инструкции."""
    assert RequestItem(**_item(unit=unit)).unit == unit


def test_request_unit_instruction_and_clause_instruction():
    metric = RequestItem.model_fields["metric"].description
    assert "Долю или процент обычно не запрашивай" in metric and "Доли и проценты не запрашиваются" not in metric
    clause = RequestItem.model_fields["clause"].description
    assert "Если у строки несколько пунктов, выбери тот, о котором именно этот показатель" in clause


def test_open_question_item():
    """Ж2: открытый вопрос — без единицы и разрезов, пункт и строка карты обязательны."""
    it = RequestItem(**_item(kind="открытый вопрос", metric="В каких случаях привлекаются внештатные эксперты?",
                             unit=None, clause="п. 2.7.1.2"))
    assert it.kind == "открытый вопрос"


def test_fresh_numbering_orders_questions_last():
    """Ж2: в ответе модели сначала показатели, затем перечни, затем вопросы."""
    q = _item(num="2", kind="открытый вопрос", metric="В каких случаях?", unit=None, clause="п. 1")
    lst = _item(num="2", kind="перечень или реестр", metric="перечень отказов (п. 1)", unit=None)
    assert RequestOutput(items=[_item(num="1"), {**lst}, {**q, "num": "3"}], years=[2025])
    with pytest.raises(ValidationError):
        RequestOutput(items=[_item(num="1"), {**q}, {**lst, "num": "3"}], years=[2025])


# ---- Индикатор 1: артефакт проверенных документов и рубрика вердикта (13.10, разбор узлов) ----


def _doc(held: bool, requested: bool) -> dict:
    return {"name": "документ", "clause": "п. 1", "held_by_state": held, "requested_by_body": requested}


def _ind01(verdict: str, docs: list, **kw) -> dict:
    extra = {"missing_data": "перечень в акте Б"} if verdict == "нет данных" else {}
    return {"indicator": 1, "name": "межведомственное взаимодействие", "checked_documents": docs,
            "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B, **extra, **kw}


def test_verdict_reasoning_before_choice():
    """Сначала разбор, потом вердикт — модель пишет JSON по порядку полей (решение владельца 13.10)."""
    from app.contracts import Indicator01Output
    order = list(Verdict.model_json_schema()["properties"])
    assert order.index("rationale") < order.index("verdict")
    order = list(Indicator01Output.model_json_schema()["properties"])
    assert order.index("checked_documents") < order.index("rationale") < order.index("verdict")


@pytest.mark.parametrize("verdict, docs", [
    ("нарушен", [_doc(True, False), _doc(False, False)]),
    ("соблюдён", [_doc(True, True), _doc(False, False)]),
    ("не применимо", []),
    ("нет данных", []),
    ("нет данных", [_doc(False, False)]),
])
def test_indicator01_verdict_follows_documents(verdict, docs):
    from app.contracts import Indicator01Output
    assert Indicator01Output(**_ind01(verdict, docs)).verdict == verdict


@pytest.mark.parametrize("verdict, docs", [
    ("соблюдён", [_doc(True, False)]),        # документ есть у государства, оговорки нет — это «нарушен»
    ("нарушен", [_doc(True, True)]),           # нарушающего документа нет
    ("не применимо", [_doc(False, False)]),    # документы подаются — это не «не применимо»
    ("соблюдён", []),                          # документов нет — это не «соблюдён»
    ("нет данных", [_doc(True, False)]),       # нарушение уже найдено
])
def test_indicator01_verdict_contradicting_documents_rejected(verdict, docs):
    from app.contracts import Indicator01Output
    with pytest.raises(ValidationError):
        Indicator01Output(**_ind01(verdict, docs))


# ---- Индикатор 2: таблица подач документов и рубрика вердикта (13.10, разбор узлов) ----


def _sub(method: str) -> dict:
    return {"what": "заявление", "clause": "п. 1", "method": method}


def _ind02(verdict: str, subs: list) -> dict:
    extra = {"missing_data": "порядок в акте Б"} if verdict == "нет данных" else {}
    return {"indicator": 2, "name": "электронная подача документов", "submissions": subs,
            "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B, **extra}


@pytest.mark.parametrize("verdict, subs", [
    ("нарушен", [_sub("только на бумаге"), _sub("только электронно")]),
    ("нарушен", [_sub("способ не назван")]),                     # решение владельца 13.10
    ("нарушен", [_sub("электронно, но обязательна бумага")]),
    ("нарушен", [_sub("электронно, но не для всех заявителей")]),
    ("соблюдён", [_sub("только электронно"), _sub("электронно или на бумаге — на выбор")]),
    ("не применимо", []),
    ("нет данных", []),
])
def test_indicator02_verdict_follows_submissions(verdict, subs):
    from app.contracts import Indicator02Output
    assert Indicator02Output(**_ind02(verdict, subs)).verdict == verdict


@pytest.mark.parametrize("verdict, subs", [
    ("соблюдён", [_sub("способ не назван")]),
    ("нарушен", [_sub("только электронно")]),
    ("не применимо", [_sub("только электронно")]),
    ("соблюдён", []),
    ("нет данных", [_sub("только на бумаге")]),
    ("нет данных", [_sub("только электронно")]),   # подачи в тексте есть и все электронные — «соблюдён»
])
def test_indicator02_verdict_contradicting_submissions_rejected(verdict, subs):
    from app.contracts import Indicator02Output
    with pytest.raises(ValidationError):
        Indicator02Output(**_ind02(verdict, subs))


def test_indicator02_reasoning_before_choice():
    from app.contracts import Indicator02Output
    order = list(Indicator02Output.model_json_schema()["properties"])
    assert order.index("submissions") < order.index("rationale") < order.index("verdict")


# ---- Индикатор 3: таблица ссылок акта и рубрика вердикта (13.10, разбор узлов) ----


def _ref(status: str) -> dict:
    return {"kind": "акт", "name": "закон № 1", "clause": "п. 1", "found": "в базе", "status": status}


def _ind03(verdict: str, refs: list) -> dict:
    extra = {"missing_data": "статус закона № 1"} if verdict == "нет данных" else {}
    return {"indicator": 3, "name": "актуальность законодательства", "references": refs,
            "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B, **extra}


@pytest.mark.parametrize("verdict, refs", [
    ("нарушен", [_ref("устарело"), _ref("не проверено"), _ref("актуально")]),
    ("нет данных", [_ref("не проверено"), _ref("актуально")]),
    ("соблюдён", [_ref("актуально"), _ref("актуально")]),
    ("не применимо", []),
])
def test_indicator03_verdict_follows_references(verdict, refs):
    from app.contracts import Indicator03Output
    assert Indicator03Output(**_ind03(verdict, refs)).verdict == verdict


@pytest.mark.parametrize("verdict, refs", [
    ("соблюдён", [_ref("актуально"), _ref("не проверено")]),   # непроверенная ссылка — не «соблюдён»
    ("нет данных", [_ref("устарело"), _ref("не проверено")]),
    ("нарушен", [_ref("актуально")]),
    ("соблюдён", []),
    ("нет данных", []),
    ("не применимо", [_ref("актуально")]),
])
def test_indicator03_verdict_contradicting_references_rejected(verdict, refs):
    from app.contracts import Indicator03Output
    with pytest.raises(ValidationError):
        Indicator03Output(**_ind03(verdict, refs))


def test_indicator03_reasoning_before_choice():
    from app.contracts import CheckedReference, Indicator03Output
    order = list(Indicator03Output.model_json_schema()["properties"])
    assert order.index("references") < order.index("rationale") < order.index("verdict")
    ref = list(CheckedReference.model_json_schema()["properties"])
    assert ref.index("found") < ref.index("status")   # сначала что показал источник, потом статус


# ---- Индикатор 4: таблица требований и двойников, рубрика вердикта (13.10, разбор узлов) ----


def _cmp(relation: str) -> dict:
    return {"requirement": "срок 30 дней", "clause": "п. 1", "counterpart": "закон № 1, ст. 2",
            "comparison": "тексты совпадают", "relation": relation}


def _ind04(verdict: str, rows: list) -> dict:
    extra = {"missing_data": "текст закона № 1"} if verdict == "нет данных" else {}
    return {"indicator": 4, "name": "отсутствие дублирования требований", "compared": rows,
            "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B, **extra}


@pytest.mark.parametrize("verdict, rows", [
    ("нарушен", [_cmp("повтор"), _cmp("не сверено")]),
    ("нарушен", [_cmp("требует и сам получает"), _cmp("ссылка")]),
    ("нет данных", [_cmp("не сверено"), _cmp("конкретизация")]),
    ("соблюдён", [_cmp("ссылка"), _cmp("конкретизация"), _cmp("воспроизведение по закону")]),
    ("соблюдён", []),          # двойников нет, но требования в акте есть
    ("не применимо", []),
])
def test_indicator04_verdict_follows_compared(verdict, rows):
    from app.contracts import Indicator04Output
    assert Indicator04Output(**_ind04(verdict, rows)).verdict == verdict


@pytest.mark.parametrize("verdict, rows", [
    ("соблюдён", [_cmp("ссылка"), _cmp("не сверено")]),
    ("нет данных", [_cmp("повтор")]),
    ("нарушен", [_cmp("конкретизация")]),
    ("не применимо", [_cmp("ссылка")]),
    ("нет данных", []),
])
def test_indicator04_verdict_contradicting_compared_rejected(verdict, rows):
    from app.contracts import Indicator04Output
    with pytest.raises(ValidationError):
        Indicator04Output(**_ind04(verdict, rows))


def test_indicator04_reasoning_before_choice():
    from app.contracts import ComparedRequirement, Indicator04Output
    order = list(Indicator04Output.model_json_schema()["properties"])
    assert order.index("compared") < order.index("rationale") < order.index("verdict")
    row = list(ComparedRequirement.model_json_schema()["properties"])
    assert row.index("comparison") < row.index("relation")   # сначала сравнение, потом вывод


# ---- Индикатор 5: таблица отказов из-за документов, рубрика вердикта (13.10, разбор узлов) ----


def _ref5(fix: str) -> dict:
    return {"ground": "неполный комплект", "clause": "п. 1", "consequence": "отказ в приёме", "fix": fix}


def _ind05(verdict: str, rows: list) -> dict:
    extra = {"missing_data": "порядок в акте Б"} if verdict == "нет данных" else {}
    return {"indicator": 5, "name": "возможность доработки пакета документов", "refusals": rows,
            "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B, **extra}


@pytest.mark.parametrize("verdict, rows", [
    ("нарушен", [_ref5("только подать заново"), _ref5("исправить в той же заявке")]),
    ("нарушен", [_ref5("исправить нельзя")]),
    ("соблюдён", [_ref5("исправить в той же заявке")]),
    ("не применимо", []),
    ("нет данных", []),
])
def test_indicator05_verdict_follows_refusals(verdict, rows):
    from app.contracts import Indicator05Output
    assert Indicator05Output(**_ind05(verdict, rows)).verdict == verdict


@pytest.mark.parametrize("verdict, rows", [
    ("соблюдён", [_ref5("только подать заново")]),
    ("нарушен", [_ref5("исправить в той же заявке")]),
    ("соблюдён", []),
    ("нет данных", [_ref5("исправить в той же заявке")]),
])
def test_indicator05_verdict_contradicting_refusals_rejected(verdict, rows):
    from app.contracts import Indicator05Output
    with pytest.raises(ValidationError):
        Indicator05Output(**_ind05(verdict, rows))


def test_indicator05_reasoning_before_choice():
    from app.contracts import DocumentRefusal, Indicator05Output
    order = list(Indicator05Output.model_json_schema()["properties"])
    assert order.index("refusals") < order.index("rationale") < order.index("verdict")
    row = list(DocumentRefusal.model_json_schema()["properties"])
    assert row.index("fix_where") < row.index("fix")


# ---- Индикатор 6: таблица требований, срок действия, рубрика вердикта (13.10, разбор узлов) ----


def _req6(kind: str) -> dict:
    return {"requirement": "соблюдать расстояние", "clause": "п. 1", "basis": "норма акта", "kind": kind}


def _ind06(verdict: str, rows: list, term: str | None, term_check: str) -> dict:
    return {"indicator": 6, "name": "наличие срока действия требования", "requirements": rows, "term": term,
            "term_check": term_check, "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B}


@pytest.mark.parametrize("verdict, rows, term, term_check", [
    ("нарушен", [_req6("устанавливает сам")], None, "срока нет"),
    ("нарушен", [_req6("устанавливает сам")], "действует до 2040 г. (п. 5)", "больше 6 лет без объяснения"),
    ("соблюдён", [_req6("устанавливает сам"), _req6("разъясняет")], "действует до 2030 г. (п. 5)", "до 6 лет"),
    ("не применимо", [_req6("разъясняет"), _req6("не обязательное требование")], None, "срока нет"),
    ("не применимо", [], None, "срока нет"),
])
def test_indicator06_verdict_follows_requirements_and_term(verdict, rows, term, term_check):
    from app.contracts import Indicator06Output
    assert Indicator06Output(**_ind06(verdict, rows, term, term_check)).verdict == verdict


@pytest.mark.parametrize("verdict, rows, term, term_check", [
    ("соблюдён", [_req6("устанавливает сам")], None, "срока нет"),
    ("нарушен", [_req6("разъясняет")], None, "срока нет"),
    ("нарушен", [_req6("устанавливает сам")], "действует до 2030 г.", "до 6 лет"),
    ("нарушен", [_req6("устанавливает сам")], "действует до 2030 г.", "срока нет"),   # term и оценка расходятся
    ("нарушен", [_req6("устанавливает сам")], None, "до 6 лет"),
])
def test_indicator06_verdict_contradicting_table_rejected(verdict, rows, term, term_check):
    from app.contracts import Indicator06Output
    with pytest.raises(ValidationError):
        Indicator06Output(**_ind06(verdict, rows, term, term_check))


def test_indicator06_reasoning_before_choice():
    from app.contracts import ActRequirement, Indicator06Output
    order = list(Indicator06Output.model_json_schema()["properties"])
    assert order.index("requirements") < order.index("term") < order.index("term_check") < order.index("rationale") < order.index("verdict")
    row = list(ActRequirement.model_json_schema()["properties"])
    assert row.index("basis") < row.index("kind")


# ---- Индикатор 7: таблица возможных повторов, рубрика вердикта (13.10, разбор узлов) ----


def _rep7(outcome: str) -> dict:
    return {"step": "проверка заявки", "clause": "п. 2", "first_clause": "п. 1",
            "difference": "те же требования", "outcome": outcome}


def _ind07(verdict: str, rows: list, procedures: str | None) -> dict:
    return {"indicator": 7, "name": "отсутствие избыточных процедур", "procedures": procedures, "repeats": rows,
            "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B}


@pytest.mark.parametrize("verdict, rows, procedures", [
    ("нарушен", [_rep7("явный повтор"), _rep7("не повтор")], "приём и проверка заявки (п. 1–2)"),
    ("соблюдён", [_rep7("не повтор")], "приём и проверка заявки (п. 1–2)"),
    ("соблюдён", [], "приём заявки (п. 1)"),
    ("не применимо", [], None),
])
def test_indicator07_verdict_follows_repeats(verdict, rows, procedures):
    from app.contracts import Indicator07Output
    assert Indicator07Output(**_ind07(verdict, rows, procedures)).verdict == verdict


@pytest.mark.parametrize("verdict, rows, procedures", [
    ("соблюдён", [_rep7("явный повтор")], "п. 1–2"),
    ("нарушен", [_rep7("не повтор")], "п. 1–2"),
    ("не применимо", [], "п. 1"),
    ("соблюдён", [], None),
    ("нарушен", [_rep7("явный повтор")], None),   # повтор без процедур — противоречие
])
def test_indicator07_verdict_contradicting_repeats_rejected(verdict, rows, procedures):
    from app.contracts import Indicator07Output
    with pytest.raises(ValidationError):
        Indicator07Output(**_ind07(verdict, rows, procedures))


def test_indicator07_reasoning_before_choice():
    from app.contracts import Indicator07Output, RepeatedStep
    order = list(Indicator07Output.model_json_schema()["properties"])
    assert order.index("procedures") < order.index("repeats") < order.index("rationale") < order.index("verdict")
    row = list(RepeatedStep.model_json_schema()["properties"])
    assert row.index("difference") < row.index("outcome")


# ---- Индикатор 8: таблица документов пакета, рубрика вердикта (13.10, разбор узлов) ----


def _doc8(mark: str) -> dict:
    return {"name": "гарантийное письмо", "clause": "п. 1", "made_by": "сам заявитель", "overlap": "нет", "mark": mark}


def _ind08(verdict: str, rows: list) -> dict:
    return {"indicator": 8, "name": "оптимальный пакет документов", "documents": rows,
            "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B}


@pytest.mark.parametrize("verdict, rows", [
    ("нарушен", [_doc8("повторяет другой"), _doc8("сам, нужен")]),
    ("нарушен", [_doc8("не сам без причины")]),
    ("соблюдён", [_doc8("сам, нужен")]),
    ("не применимо", []),
])
def test_indicator08_verdict_follows_documents(verdict, rows):
    from app.contracts import Indicator08Output
    assert Indicator08Output(**_ind08(verdict, rows)).verdict == verdict


@pytest.mark.parametrize("verdict, rows", [
    ("соблюдён", [_doc8("повторяет другой")]),
    ("нарушен", [_doc8("сам, нужен")]),
    ("соблюдён", []),
    ("не применимо", [_doc8("сам, нужен")]),
])
def test_indicator08_verdict_contradicting_documents_rejected(verdict, rows):
    from app.contracts import Indicator08Output
    with pytest.raises(ValidationError):
        Indicator08Output(**_ind08(verdict, rows))


def test_indicator08_reasoning_before_choice():
    from app.contracts import Indicator08Output, PackageDocument
    order = list(Indicator08Output.model_json_schema()["properties"])
    assert order.index("documents") < order.index("rationale") < order.index("verdict")
    row = list(PackageDocument.model_json_schema()["properties"])
    assert row.index("made_by") < row.index("mark") and row.index("overlap") < row.index("mark")


# ---- Индикатор 9: таблица возможно неясных формулировок, рубрика вердикта (13.10, разбор узлов) ----


def _w9(mark: str) -> dict:
    return {"clause": "п. 1", "quote": "при необходимости", "readings": "два прочтения",
            "effect": "меняется пакет", "mark": mark}


def _ind09(verdict: str, rows: list, addressed: str | None) -> dict:
    return {"indicator": 9, "name": "содержание понятных требований", "addressed": addressed, "wordings": rows,
            "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B}


@pytest.mark.parametrize("verdict, rows, addressed", [
    ("нарушен", [_w9("непонятно"), _w9("понятно")], "требования к заявителю (п. 1–3)"),
    ("соблюдён", [_w9("понятно")], "требования к заявителю (п. 1–3)"),
    ("соблюдён", [], "требования к заявителю (п. 1)"),
    ("не применимо", [], None),
])
def test_indicator09_verdict_follows_wordings(verdict, rows, addressed):
    from app.contracts import Indicator09Output
    assert Indicator09Output(**_ind09(verdict, rows, addressed)).verdict == verdict


@pytest.mark.parametrize("verdict, rows, addressed", [
    ("соблюдён", [_w9("непонятно")], "п. 1"),
    ("нарушен", [_w9("понятно")], "п. 1"),
    ("не применимо", [], "п. 1"),
    ("соблюдён", [], None),
    ("нарушен", [_w9("непонятно")], None),
])
def test_indicator09_verdict_contradicting_wordings_rejected(verdict, rows, addressed):
    from app.contracts import Indicator09Output
    with pytest.raises(ValidationError):
        Indicator09Output(**_ind09(verdict, rows, addressed))


def test_indicator09_reasoning_before_choice():
    from app.contracts import Indicator09Output, UnclearWording
    order = list(Indicator09Output.model_json_schema()["properties"])
    assert order.index("addressed") < order.index("wordings") < order.index("rationale") < order.index("verdict")
    row = list(UnclearWording.model_json_schema()["properties"])
    assert row.index("effect") < row.index("mark")


# ---- Индикатор 10: применимость и таблица фактов практики, рубрика вердикта (14.10, разбор узлов) ----


def _f10(mark: str) -> dict:
    return {"source": "https://example.ru/a", "fact": "отбор не состоялся", "about": "этот акт", "mark": mark}


def _ind10(verdict: str, rows: list, applicability: str = "применим") -> dict:
    extra = ({"missing_data": "нет сведений о заявках",
              "to_request": [{"metric": "число поданных заявок", "unit": "ед.", "clause": "п. 2"}]}
             if verdict == "нет данных" else {})
    return {"indicator": 10, "name": "учёт практики применения", "applicability_basis": "акт действует",
            "applicability": applicability, "searches": ["суды", "новости", "доклады"], "findings": rows,
            "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B, **extra}


@pytest.mark.parametrize("verdict, rows, applicability", [
    ("нарушен", [_f10("не работает"), _f10("работает")], "применим"),
    ("соблюдён", [_f10("работает"), _f10("не про этот акт")], "применим"),
    ("нет данных", [_f10("не про этот акт")], "применим"),
    ("нет данных", [], "применим"),
    ("не применимо", [], "не вступил в силу"),
    ("не применимо", [_f10("не работает")], "только органу"),
])
def test_indicator10_verdict_follows_findings(verdict, rows, applicability):
    from app.contracts import Indicator10Output
    assert Indicator10Output(**_ind10(verdict, rows, applicability)).verdict == verdict


@pytest.mark.parametrize("verdict, rows, applicability", [
    ("нет данных", [_f10("не работает")], "применим"),
    ("соблюдён", [], "применим"),
    ("нарушен", [_f10("не про этот акт")], "применим"),
    ("нет данных", [], "не вступил в силу"),
])
def test_indicator10_verdict_contradicting_findings_rejected(verdict, rows, applicability):
    from app.contracts import Indicator10Output
    with pytest.raises(ValidationError):
        Indicator10Output(**_ind10(verdict, rows, applicability))


def test_indicator10_reasoning_before_choice():
    from app.contracts import Indicator10Output, PracticeFinding
    order = list(Indicator10Output.model_json_schema()["properties"])
    assert order.index("applicability_basis") < order.index("applicability") < order.index("findings") \
        < order.index("rationale") < order.index("verdict")
    row = list(PracticeFinding.model_json_schema()["properties"])
    assert row.index("about") < row.index("mark")


# ---- Индикатор 11: таблица сроков, разнобой единиц, рубрика вердикта (14.10, разбор узлов) ----


def _d11(mark: str) -> dict:
    return {"whose": "орган", "what": "рассмотрение", "value": "42 рабочих дня", "clause": "п. 1",
            "federal": "174-ФЗ, ст. 14, не более 42 рабочих дней, https://www.consultant.ru/x", "mark": mark}


def _ind11(verdict: str, rows: list, units_mixed: str | None = None) -> dict:
    return {"indicator": 11, "name": "приемлемые сроки", "deadlines": rows, "units_mixed": units_mixed,
            "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B}


@pytest.mark.parametrize("verdict, rows, units_mixed", [
    ("нарушен", [_d11("длиннее федерального")], None),
    ("нарушен", [_d11("максимум при делении по сложности"), _d11("в пределах")], None),
    ("нарушен", [_d11("в пределах")], "п. 1 — рабочие, п. 2 — календарные"),
    ("соблюдён", [_d11("в пределах")], None),
    ("не применимо", [], None),
])
def test_indicator11_verdict_follows_deadlines(verdict, rows, units_mixed):
    from app.contracts import Indicator11Output
    assert Indicator11Output(**_ind11(verdict, rows, units_mixed)).verdict == verdict


@pytest.mark.parametrize("verdict, rows, units_mixed", [
    ("соблюдён", [_d11("длиннее федерального")], None),
    ("соблюдён", [_d11("в пределах")], "п. 1 — рабочие, п. 2 — календарные"),
    ("нарушен", [_d11("в пределах")], None),
    ("соблюдён", [], None),
    ("нарушен", [], "п. 1 — рабочие"),
])
def test_indicator11_verdict_contradicting_deadlines_rejected(verdict, rows, units_mixed):
    from app.contracts import Indicator11Output
    with pytest.raises(ValidationError):
        Indicator11Output(**_ind11(verdict, rows, units_mixed))


def test_indicator11_reasoning_before_choice():
    from app.contracts import DeadlineCheck, Indicator11Output
    order = list(Indicator11Output.model_json_schema()["properties"])
    assert order.index("deadlines") < order.index("units_mixed") < order.index("rationale") < order.index("verdict")
    row = list(DeadlineCheck.model_json_schema()["properties"])
    assert row.index("federal") < row.index("mark")


@pytest.mark.parametrize("federal, ok", [
    ("нет", True), ("Нет.", True), ("174-ФЗ, ст. 14, https://www.consultant.ru/x", True),
    ("248-ФЗ, ст. 49 — 20 рабочих дней (по памяти)", False),
])
def test_indicator11_federal_norm_only_from_read_page(federal, ok):
    """14.10: федеральная норма — только с адресом прочитанной страницы, иначе «нет»."""
    from app.contracts import DeadlineCheck
    row = {**_d11("в пределах"), "federal": federal}
    if ok:
        DeadlineCheck(**row)
    else:
        with pytest.raises(ValidationError):
            DeadlineCheck(**row)


def test_indicator11_person_deadline_never_violation():
    """14.10: длину сравниваем только у сроков органа — срок лица всегда «в пределах»."""
    from app.contracts import DeadlineCheck
    DeadlineCheck(**{**_d11("в пределах"), "whose": "лицо"})
    with pytest.raises(ValidationError):
        DeadlineCheck(**{**_d11("длиннее федерального"), "whose": "лицо"})


# ---- Индикатор 12: таблица условий допуска и назначений, рубрика вердикта (14.10, разбор узлов) ----


def _l12(mark: str) -> dict:
    return {"clause": "п. 1", "what": "вакцинация — только в ГБУ", "who_pays": "само лицо",
            "assessment": "лицо не выбирает исполнителя", "mark": mark}


def _ind12(verdict: str, rows: list, allocation: str | None) -> dict:
    return {"indicator": 12, "name": "отсутствие ограничения конкуренции", "allocation": allocation, "limits": rows,
            "rationale": "полный разбор", "verdict": verdict, "rationale_brief": _B}


@pytest.mark.parametrize("verdict, rows, allocation", [
    ("нарушен", [_l12("ограничивает"), _l12("не ограничивает")], "назначение исполнителя (п. 1)"),
    ("соблюдён", [_l12("не ограничивает")], "отбор (п. 2)"),
    ("соблюдён", [], "отбор (п. 2)"),
    ("не применимо", [], None),
])
def test_indicator12_verdict_follows_limits(verdict, rows, allocation):
    from app.contracts import Indicator12Output
    assert Indicator12Output(**_ind12(verdict, rows, allocation)).verdict == verdict


@pytest.mark.parametrize("verdict, rows, allocation", [
    ("соблюдён", [_l12("ограничивает")], "п. 1"),
    ("нарушен", [_l12("не ограничивает")], "п. 1"),
    ("не применимо", [], "п. 1"),
    ("соблюдён", [], None),
    ("нарушен", [_l12("ограничивает")], None),
])
def test_indicator12_verdict_contradicting_limits_rejected(verdict, rows, allocation):
    from app.contracts import Indicator12Output
    with pytest.raises(ValidationError):
        Indicator12Output(**_ind12(verdict, rows, allocation))


def test_indicator12_reasoning_before_choice():
    from app.contracts import CompetitionLimit, Indicator12Output
    order = list(Indicator12Output.model_json_schema()["properties"])
    assert order.index("allocation") < order.index("limits") < order.index("rationale") < order.index("verdict")
    row = list(CompetitionLimit.model_json_schema()["properties"])
    assert row.index("assessment") < row.index("mark")


# ---- 14.10, ошибка 1 аудита: гипотеза не может молча пропасть ----


def test_hypothesis_set_output_rejects_empty_list():
    """В ответе модели гипотез хотя бы одна: сломанный ответ (гипотеза внутри текста why_this_many,
    список пуст — 646-ПП 13.10) отвергается и переспрашивается. Пустой набор хранения — только у сбоя узла."""
    with pytest.raises(ValidationError):
        HypothesisSetOutput(indicator=1, name="н", violation_kind="издержки",
                            why_this_many='одна",\n<parameter name="hypotheses">[...]', hypotheses=[])
    from app.contracts import HypothesisSet
    assert HypothesisSet(indicator=1, name="н", why_this_many="сбой вызова", hypotheses=[]).hypotheses == []


def test_indicator10_to_request_only_and_always_with_no_data():
    """14.10: числа для органа — списком, только и обязательно при «нет данных»."""
    from app.contracts import Indicator10Output
    with pytest.raises(ValidationError):
        Indicator10Output(**{**_ind10("нет данных", []), "to_request": []})
    with pytest.raises(ValidationError):
        Indicator10Output(**{**_ind10("нарушен", [_f10("не работает")]),
                             "to_request": [{"metric": "число заявок", "unit": "ед.", "clause": "п. 1"}]})
    order = list(Indicator10Output.model_json_schema()["properties"])
    assert order.index("findings") < order.index("to_request") < order.index("verdict")


# ---- 14.10, ошибка 4 аудита: вопросы органу — своим списком в ответе гипотез ----


def test_hypothesis_output_separates_numbers_and_questions():
    """Число — в data («открытый» / «запрос органу»), вопрос словами — в questions; вопрос в data не принимается."""
    from app.contracts import HypothesisOutput
    ok = {**_hyp_out(), "questions": [{"clause": "п. 2.7", "why": "понять причины продлений",
                                        "question": "По каким основаниям чаще всего продлевается срок?"}]}
    assert len(HypothesisOutput(**ok).questions) == 1
    assert HypothesisOutput(**_hyp_out()).questions == []                       # без вопросов — нормально
    bad = {**_hyp_out(), "data": [{"metric": "Почему?", "source": "вопрос органу"}]}
    with pytest.raises(ValidationError):
        HypothesisOutput(**bad)


# ---- 14.10, карта: своя цитата у гипотезы, мягкая сверка с актом ----

_ACT = ("1.2. Институты развития, в том числе АО «Российская венчурная компания», вправе выдавать займы. "
        "1.3.10. Получатель субсидии не является аффилированным лицом банка или лизингодателя.")


@pytest.mark.parametrize("quote, ok", [
    ("Получатель субсидии не является аффилированным лицом банка или лизингодателя.", True),
    ("получатель субсидии не являеться аффилированным лицом банка или лизингодателя", True),   # опечатка
    ("Институты развития, в том числе АО \"Российская венчурная компания\", вправе выдавать займы", True),
    ("Институты развития … вправе выдавать займы", True),                                      # сокращение
    ("Банк сам решает, кого считать аффилированным, критериев в акте нет", False),             # пересказ
])
def test_quote_in_text_soft(quote, ok):
    from app.contracts import quote_in_text
    assert quote_in_text(quote, _ACT) is ok


def test_hypothesis_proof_checked_against_act_only_with_context():
    """Цитата сверяется с текстом акта, когда узел передал его в контексте; без контекста — нет."""
    from app.contracts import HypothesisOutput
    bad = {**_hyp_out(), "proof": "Банк сам решает, кого считать аффилированным, критериев в акте нет"}
    HypothesisOutput(**bad)                                                    # без контекста проходит
    with pytest.raises(ValidationError):
        HypothesisOutput.model_validate(bad, context={"act_text": _ACT})
    good = {**_hyp_out(), "proof": "не является аффилированным лицом банка"}
    assert HypothesisOutput.model_validate(good, context={"act_text": _ACT}).proof
