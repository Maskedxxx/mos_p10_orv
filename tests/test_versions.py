"""
Тесты app/versions.py — версии прогона в диалоговом режиме (план итерации 3, шаг 2).
"""

from __future__ import annotations

import json

import pytest

from app import versions
from app.contracts import (
    Buckets, DataItem, DataRequest, Effect, HypothesisMap, JournalEntry, MapRow, RequestItem, SourceRef, Verdict,
)
from app.trace import run_dir, save_json

_EFFECT = Effect(affected="бизнес", metric="издержки, руб.", how_to_calculate="сумма", expected_trend="рост")


def _write_run(rid: str) -> None:
    """Минимальный завершённый прогон: 12 вердиктов, кучи, карта из двух строк, запрос, Excel."""
    verdicts = [Verdict(indicator=n, name=f"индикатор {n}", verdict="соблюдён", rationale="ok") for n in range(1, 13)]
    verdicts[2] = Verdict(indicator=3, name="индикатор 3", verdict="нарушен", rationale="плохо")
    verdicts[3] = Verdict(indicator=4, name="индикатор 4", verdict="нет данных", rationale="не открылся закон",
                          missing_data="текст закона")
    for v in verdicts:
        save_json(rid, f"03_verdicts/{v.indicator:02d}.json", v)
        (run_dir(rid) / "03_verdicts" / f"{v.indicator:02d}.raw.json").write_text("{}", encoding="utf-8")
    save_json(rid, "04_buckets.json", Buckets(violated=[verdicts[2]], no_data=[verdicts[3]],
                                             checked=[v for v in verdicts if v.verdict == "соблюдён"]))
    rows = [MapRow(row_id=i, indicators=[3], hypothesis=f"г{i}", grounds="о", effects=[_EFFECT],
                   data=[DataItem(metric=f"п{i}", source="запрос органу")]) for i in (1, 2)]
    save_json(rid, "06_map.json", HypothesisMap(rows=rows))
    save_json(rid, "07_request.json", DataRequest(items=[
        RequestItem(num="1", kind="показатель по годам", metric="п1 (п. 1)", unit="ед.", for_rows=[1],
                    sources=[SourceRef(row_id=1, metric="п1")])], years=[2025]))
    (run_dir(rid) / "08_result.xlsx").write_bytes(b"xlsx-v0")
    (run_dir(rid) / "run.log").write_text("лог", encoding="utf-8")


@pytest.fixture
def rid(tmp_runs_dir) -> str:
    _write_run("run_versions")
    return "run_versions"


def _entry(version: int) -> JournalEntry:
    return JournalEntry(version=version, at="2026-09-26T10:00:00", kind="правка", target="Карта гипотез, стр. 1",
                        action="переформулировано", before="г1", after="г1 короче", reason="вода")


def test_v0_created_once_with_clean_artifacts_only(rid):
    state = versions.ensure_v0(rid)
    v0 = run_dir(rid) / "versions" / "v0"
    assert state.current == 0
    assert (v0 / "06_map.json").exists() and (v0 / "03_verdicts" / "03.json").exists()
    assert not (v0 / "03_verdicts" / "03.raw.json").exists()     # сырые ответы — не часть версии
    assert not (v0 / "run.log").exists() and not (v0 / "meta.json").exists()
    before = (v0 / "06_map.json").read_bytes()
    (run_dir(rid) / "06_map.json").write_text("{}", encoding="utf-8")
    assert versions.ensure_v0(rid).current == 0                   # второй вызов ничего не пересоздаёт
    assert (v0 / "06_map.json").read_bytes() == before


def test_state_counters_start_after_existing_numbers(rid):
    state = versions.ensure_v0(rid)
    assert state.unverified_ids == {4: 3}        # «нет данных» без строки карты — строка 3
    assert state.next_row_id == 4                # после строк 1, 2 и строки «проверка не завершена» 3
    assert state.next_item_num == 2


def test_commit_snapshots_root_and_appends_journal(rid):
    state = versions.ensure_v0(rid)
    (run_dir(rid) / "08_result.xlsx").write_bytes(b"xlsx-v1")
    state = versions.commit(rid, state, [_entry(1)])
    assert state.current == 1
    assert (run_dir(rid) / "versions" / "v1" / "08_result.xlsx").read_bytes() == b"xlsx-v1"
    assert (run_dir(rid) / "versions" / "v0" / "08_result.xlsx").read_bytes() == b"xlsx-v0"
    state = versions.commit(rid, state, [_entry(2)])
    assert [e.version for e in versions.read_journal(rid)] == [1, 2]
    assert versions.load_state(rid).current == 2


def test_commit_rejects_entry_of_other_version(rid):
    state = versions.ensure_v0(rid)
    with pytest.raises(ValueError):
        versions.commit(rid, state, [_entry(5)])


def test_restore_returns_root_byte_for_byte_and_drops_new_files(rid):
    state = versions.ensure_v0(rid)
    original = (run_dir(rid) / "06_map.json").read_bytes()
    (run_dir(rid) / "06_map.json").write_text("{}", encoding="utf-8")
    save_json(rid, "05_hypotheses/07.json", {"x": 1})             # файл, которого не было в версии
    versions.restore(rid, 0)
    assert (run_dir(rid) / "06_map.json").read_bytes() == original
    assert not (run_dir(rid) / "05_hypotheses" / "07.json").exists()
    assert (run_dir(rid) / "03_verdicts" / "03.raw.json").exists()  # сырые ответы не трогаются


def test_recover_after_crash_mid_step(rid):
    versions.ensure_v0(rid)
    assert versions.recover(rid) is False                        # корень совпадает с текущей версией
    (run_dir(rid) / "07_request.json").write_text("{}", encoding="utf-8")   # сбой посреди шага
    assert versions.recover(rid) is True
    assert json.loads((run_dir(rid) / "07_request.json").read_text(encoding="utf-8"))["items"][0]["num"] == "1"



def test_version_commit_and_restore_are_logged(rid):
    """Журнал сессии (2.7): новая версия и возврат к версии — события с номером и тем, что изменено."""
    from app.trace import read_events
    state = versions.ensure_v0(rid)
    versions.commit(rid, state, [_entry(1)])
    versions.restore(rid, 0)
    created = [e for e in read_events(rid) if e["event"] == "version_created"]
    assert [e["version"] for e in created] == [0, 1]
    assert created[1]["changes"] == ["правка: Карта гипотез, стр. 1"]
    assert [e["version"] for e in read_events(rid) if e["event"] == "version_restored"] == [0]
