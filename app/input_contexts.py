"""Typed input views. Select existing fields; never infer or rewrite their meaning.

Whole passport blocks reuse their original types. Partial blocks reuse field
types and metadata, without inheriting validators that need omitted fields.
These schemas validate preparation in Python; they are not sent to the LLM.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model

from app.contracts import (
    ActPassport, DataItem, DataRequest, Effect, Hypothesis, HypothesisMap,
    MapRow, RequestItem,
)
from app.mandatory_indicators import MandatoryMetric


class InputContext(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _subset(name: str, source: type[BaseModel], fields: tuple[str, ...]) -> type[BaseModel]:
    """Reuse field declarations, without copying the source model's validators."""
    return create_model(name, __base__=InputContext, __module__=__name__, **{
        key: (source.model_fields[key].annotation, deepcopy(source.model_fields[key]))
        for key in fields
    })


# '*' means the entire existing block, including optional fields and empty values.
# Each indicator owns its profile; identical shapes may reuse partial types.
INDICATOR_PROFILES = {
    1: {"status": "*", "addressee": "*", "nature": "*", "documents": "*",
        "procedure": ("state", "participants", "clauses")},
    2: {"status": "*", "addressee": "*", "nature": "*", "documents": "*",
        "procedure": ("state", "participants", "clauses")},
    3: {"requisites": ("kind", "number", "date", "authority", "title"),
        "status": "*", "nature": "*", "references": "*"},
    4: {"requisites": ("kind", "number", "date", "authority", "title"),
        "status": "*", "nature": "*", "requirements": "*",
        "references": ("acts",)},
    5: {"status": "*", "nature": "*", "addressee": "*", "procedure": "*",
        "documents": ("items",)},
    6: {"status": "*", "nature": "*", "addressee": "*", "requirements": "*"},
    7: {"status": "*", "nature": "*", "addressee": "*", "procedure": "*",
        "control": "*", "documents": ("items",), "deadlines": ("items",)},
    8: {"status": "*", "nature": "*", "addressee": "*", "documents": "*",
        "procedure": ("state", "participants", "result", "clauses")},
    9: {"status": "*", "nature": "*", "addressee": "*", "requirements": "*",
        "procedure": "*", "documents": "*", "deadlines": "*", "allocation": "*", "control": "*"},
    10: {"requisites": "*", "status": "*", "addressee": "*", "nature": "*",
         "requirements": "*", "procedure": "*", "control": "*", "allocation": "*"},
    11: {"requisites": ("kind", "number", "date", "authority", "title"),
         "status": "*", "nature": "*", "addressee": "*",
         "procedure": ("state", "participants", "result", "clauses"),
         "deadlines": "*", "references": ("acts",)},
    12: {"status": "*", "nature": "*", "addressee": "*", "requirements": "*",
         "procedure": ("state", "participants", "result", "clauses"), "allocation": "*"},
}
HYPOTHESES_PROFILE = {
    "requisites": ("developers",), "status": "*", "addressee": "*",
    "procedure": ("state", "participants", "result", "clauses"),
    "documents": ("items",), "deadlines": ("items",), "allocation": "*", "control": "*",
}

_partial_types: dict[tuple[type[BaseModel], tuple[str, ...]], type[BaseModel]] = {}


def _passport_model(name: str, profile: dict) -> type[BaseModel]:
    fields = {}
    for key, selection in profile.items():
        source = ActPassport.model_fields[key].annotation
        if selection == "*":
            view = source
        else:
            cache_key = (source, selection)
            if cache_key not in _partial_types:
                label = source.__name__ + "_" + "_".join(selection) + "Context"
                _partial_types[cache_key] = _subset(label, source, selection)
            view = _partial_types[cache_key]
        fields[key] = (view, ...)
    return create_model(name, __base__=InputContext, __module__=__name__, **fields)


INDICATOR_CONTEXTS = {
    n: _passport_model(f"Indicator{n:02d}PassportContext", profile)
    for n, profile in INDICATOR_PROFILES.items()
}
ViolatedHypothesesPassportContext = _passport_model("ViolatedHypothesesPassportContext", HYPOTHESES_PROFILE)
NoDataHypothesesPassportContext = _passport_model("NoDataHypothesesPassportContext", HYPOTHESES_PROFILE)
RequestPassportContext = _passport_model("RequestPassportContext", {"requisites": ("developers",), "status": "*"})


def passport_context(passport: ActPassport, model: type[BaseModel]) -> BaseModel:
    data = {}
    for key, field in model.model_fields.items():
        block = getattr(passport, key)
        data[key] = block.model_dump(mode="json", include=set(field.annotation.model_fields))
    return model.model_validate(data)


def passport_context_block(passport: ActPassport | None, model: type[BaseModel]) -> str:
    if passport is None:
        return ""
    data = passport_context(passport, model).model_dump(mode="json")
    return "## ПАСПОРТ АКТА\n\n```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```\n"


class NumberedHypothesis(Hypothesis):
    row_id: int
    indicator: int


class MergeCandidate(InputContext):
    row_ids: tuple[int, int]
    shared_clauses: list[str]


class MergeInput(InputContext):
    hypotheses: list[NumberedHypothesis]
    candidates: list[MergeCandidate]


def merge_input(flat: list[tuple[int, int, Hypothesis]], candidates: list[tuple[int, int, list[str]]]) -> MergeInput:
    return MergeInput(
        hypotheses=[NumberedHypothesis(row_id=row_id, indicator=n, **hyp.model_dump(mode="json"))
                    for row_id, n, hyp in flat],
        candidates=[MergeCandidate(row_ids=(a, b), shared_clauses=shared) for a, b, shared in candidates],
    )


RequestEffectContext = _subset("RequestEffectContext", Effect, ("affected", "metric", "how_to_calculate"))
ExistingRequestItemContext = _subset("ExistingRequestItemContext", RequestItem,
                                    ("num", "kind", "metric", "unit", "clause", "breakdown", "note"))


class RequestTargetData(DataItem):
    source: Literal["запрос органу", "вопрос органу"]


RequestMapRowContext = create_model(
    "RequestMapRowContext", __base__=_subset("RequestRowIdentity", MapRow, ("row_id", "hypothesis", "clause")),
    __module__=__name__, effects=(list[RequestEffectContext], ...), data=(list[RequestTargetData], ...),
)


class RequestInput(InputContext):
    act_text: str | None
    passport: RequestPassportContext | None
    rows: list[RequestMapRowContext]
    mandatory: list[MandatoryMetric]
    years: list[int]
    existing_items: list[ExistingRequestItemContext] | None


def request_input(hypothesis_map: HypothesisMap, years: list[int], document_md: str | None,
                  passport: ActPassport | None, existing: DataRequest | None,
                  mandatory: list[MandatoryMetric] | None) -> RequestInput:
    rows = []
    for row in hypothesis_map.rows:
        data = [d.model_dump(mode="json") for d in row.data if d.source in ("запрос органу", "вопрос органу")]
        if not data:
            continue
        rows.append(RequestMapRowContext(
            row_id=row.row_id, hypothesis=row.hypothesis, clause=row.clause,
            effects=[e.model_dump(mode="json", include=set(RequestEffectContext.model_fields)) for e in row.effects],
            data=data,
        ))
    return RequestInput(
        act_text=document_md,
        passport=passport_context(passport, RequestPassportContext) if passport is not None else None,
        rows=rows, mandatory=mandatory or [], years=years,
        existing_items=[item.model_dump(mode="json", include=set(ExistingRequestItemContext.model_fields))
                        for item in existing.items] if existing is not None else None,
    )
