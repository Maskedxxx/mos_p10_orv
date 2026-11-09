"""Input boundaries: retain agreed values, do not invent or mutate stored data."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.contracts import ActPassport, DataRequest, Hypothesis, HypothesisMap, MapRow, RequestItem
from app.input_contexts import (
    INDICATOR_CONTEXTS, NoDataHypothesesPassportContext,
    ViolatedHypothesesPassportContext, RequestPassportContext,
    passport_context, passport_context_block, merge_input, request_input,
)
from app.mandatory_indicators import MandatoryMetric


# Reviewed input schemas are the external specification, not a copy imported
# from the implementation's selection profiles.
FIELDS = json.loads((Path(__file__).parent / "fixtures/input_context_fields.json").read_text())
MODELS = {str(n): model for n, model in INDICATOR_CONTEXTS.items()} | {
    "violated": ViolatedHypothesesPassportContext,
    "no_data": NoDataHypothesesPassportContext,
    "request": RequestPassportContext,
}


@pytest.mark.parametrize("key", MODELS)
@pytest.mark.parametrize("variant", ["empty", "project", "multiple_developers"])
def test_agreed_fields_preserve_values_and_absence(key, variant, valid_passport):
    data = deepcopy(valid_passport)
    if variant == "project":
        data["requisites"].update(number=None, date=None)
        data["status"].update(status="проект", status_basis="Текст обозначен как проект; номер, дата принятия и подпись отсутствуют")
    if variant == "multiple_developers":
        data["requisites"]["developers"] = [
            {"name": "Орган А", "scope": "раздел 1", "basis": "п. 1"},
            {"name": "Орган Б", "scope": "раздел 2", "basis": "п. 2"},
        ]
    passport = ActPassport.model_validate(data)
    before = passport.model_dump(mode="json")
    view = passport_context(passport, MODELS[key]).model_dump(mode="json")
    expected = {root: {field: before[root][field] for field in fields}
                for root, fields in FIELDS[key].items()}
    assert view == expected
    assert passport.model_dump(mode="json") == before
    assert passport_context_block(None, MODELS[key]) == ""


def test_request_filter_uses_source_not_words_and_retains_row_ids(valid_map_row, valid_passport):
    rows = []
    for row_id, source in [(4, "открытый"), (17, "запрос органу"), (31, "вопрос органу")]:
        row = deepcopy(valid_map_row)
        row.update(row_id=row_id, clause=None)
        # Identical metric names must not be treated as duplicates or as a source.
        row["data"] = [{"metric": "открытый запрос органу", "source": source, "where": "https://example.org/data" if source == "открытый" else None}]
        rows.append(MapRow.model_validate(row))
    mixed = rows[1].model_copy(deep=True)
    mixed.data += rows[0].data
    hyp_map = HypothesisMap(rows=[rows[0], mixed, rows[2]], merges=[])
    before = hyp_map.model_dump(mode="json")
    packet = request_input(hyp_map, [2023, 2024], "ПОЛНЫЙ АКТ", ActPassport(**valid_passport), None, None)
    result = packet.model_dump(mode="json")
    assert [r["row_id"] for r in result["rows"]] == [17, 31]
    assert [r["data"] for r in result["rows"]] == [before["rows"][1]["data"][:1], before["rows"][2]["data"]]
    assert set(result["rows"][0]) == {"row_id", "hypothesis", "clause", "effects", "data"}
    assert set(result["rows"][0]["effects"][0]) == {"affected", "metric", "how_to_calculate"}
    assert result["act_text"] == "ПОЛНЫЙ АКТ"
    assert result["years"] == [2023, 2024]
    assert result["existing_items"] is None
    assert hyp_map.model_dump(mode="json") == before


def test_append_keeps_breakdown_note_and_mandatory(valid_request_item):
    item = RequestItem(**(valid_request_item | {"breakdown": ["основание А", "основание Б"], "note": "часть уже учтена"}))
    existing = DataRequest(items=[item], years=[2024])
    mandatory = MandatoryMetric(codes=["required"], metric="число случаев", unit="ед.", breakdown="по видам", kind="показатель по годам")
    packet = request_input(HypothesisMap(rows=[], merges=[]), [2024], None, None, existing, [mandatory])
    data = packet.model_dump(mode="json")
    assert data["passport"] is None and data["act_text"] is None and data["rows"] == []
    assert data["existing_items"] == [{k: item.model_dump(mode="json")[k]
                                       for k in ("num", "kind", "metric", "unit", "clause", "breakdown", "note")}]
    assert packet.mandatory == [mandatory]


def test_merge_keeps_every_hypothesis_field_and_candidate(valid_hypothesis):
    first = Hypothesis(**(valid_hypothesis | {"clause": "п. 4", "proof": "цитата", "hypothesis_brief": ["деталь"]}))
    second = Hypothesis(**valid_hypothesis)
    data = merge_input([(3, 1, first), (19, 5, second)], [(3, 19, ["4"])]).model_dump(mode="json")
    assert set(data) == {"hypotheses", "candidates"}
    assert data["hypotheses"] == [
        first.model_dump(mode="json") | {"row_id": 3, "indicator": 1},
        second.model_dump(mode="json") | {"row_id": 19, "indicator": 5},
    ]
    assert data["candidates"] == [{"row_ids": [3, 19], "shared_clauses": ["4"]}]
