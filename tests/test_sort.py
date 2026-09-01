"""
Тесты app/nodes/sort.py (узел [3], разбор корзины вердиктов на четыре кучи).

Чистая логика, модель не участвует — моков call() нет. Проверяется только
раскладка по кучам, сохранность (ничего не теряется), порядок и трейс.
"""

from __future__ import annotations

import pytest

from app.contracts import RunMeta, Verdict
from app.nodes.sort import sort_verdicts
from app.trace import run_dir, save_json


def _verdict(n: int, verdict: str, **extra) -> Verdict:
    """Собрать минимально валидный Verdict с заданным номером и статусом."""
    data = {
        "indicator": n,
        "name": f"индикатор {n}",
        "verdict": verdict,
        "rationale": "проверено по тексту акта",
        **extra,
    }
    if verdict == "нет данных" and "missing_data" not in data:
        data["missing_data"] = "нужен показатель X"
    return Verdict(**data)


@pytest.fixture
def run_id(tmp_runs_dir) -> str:
    """run_id с уже созданным meta.json (save_json внутри sort_verdicts его не требует,
    но так единообразно с остальными узлами и не мешает)."""
    rid = "run_sort_test"
    meta = RunMeta(
        run_id=rid, act_filename="act.docx",
        started_at="2026-01-01T12:00:00", status="started",
    )
    save_json(rid, "meta.json", meta)
    return rid


def _all_twelve_verdicts() -> list[Verdict]:
    """Двенадцать вердиктов: 3 нарушен, 2 нет данных, 5 соблюдён/не применимо, 2 ошибка."""
    return [
        _verdict(1, "нарушен"),
        _verdict(2, "соблюдён"),
        _verdict(3, "нет данных"),
        _verdict(4, "не применимо"),
        _verdict(5, "нарушен"),
        _verdict(6, "ошибка"),
        _verdict(7, "соблюдён"),
        _verdict(8, "нет данных"),
        _verdict(9, "нарушен"),
        _verdict(10, "соблюдён"),
        _verdict(11, "ошибка"),
        _verdict(12, "не применимо"),
    ]


def test_twelve_verdicts_sorted_into_correct_buckets(run_id):
    """1. Двенадцать вердиктов раскладываются правильно по кучам."""
    buckets = sort_verdicts(_all_twelve_verdicts(), run_id)

    assert {v.indicator for v in buckets.violated} == {1, 5, 9}
    assert {v.indicator for v in buckets.no_data} == {3, 8}
    assert {v.indicator for v in buckets.checked} == {2, 4, 7, 10, 12}
    assert {v.indicator for v in buckets.failed} == {6, 11}


def test_nothing_lost_sum_equals_input(run_id):
    """2. Ничего не потеряно — сумма длин четырёх куч равна числу вердиктов на входе."""
    verdicts = _all_twelve_verdicts()
    buckets = sort_verdicts(verdicts, run_id)

    total = len(buckets.violated) + len(buckets.no_data) + len(buckets.checked) + len(buckets.failed)
    assert total == len(verdicts)


def test_checked_and_not_applicable_share_one_bucket(run_id):
    """3. "соблюдён" и "не применимо" попадают в одну кучу (checked)."""
    verdicts = [_verdict(1, "соблюдён"), _verdict(2, "не применимо")]
    buckets = sort_verdicts(verdicts, run_id)

    assert {v.indicator for v in buckets.checked} == {1, 2}
    assert buckets.violated == []
    assert buckets.no_data == []
    assert buckets.failed == []


def test_error_goes_to_failed_not_checked(run_id):
    """4. "ошибка" попадает в failed, а не в checked."""
    verdicts = [_verdict(1, "ошибка")]
    buckets = sort_verdicts(verdicts, run_id)

    assert {v.indicator for v in buckets.failed} == {1}
    assert buckets.checked == []


def test_order_within_bucket_by_indicator_number(run_id):
    """5. Порядок внутри кучи — по номеру индикатора, а не по порядку входного списка."""
    verdicts = [
        _verdict(9, "нарушен"),
        _verdict(1, "нарушен"),
        _verdict(5, "нарушен"),
    ]
    buckets = sort_verdicts(verdicts, run_id)

    assert [v.indicator for v in buckets.violated] == [1, 5, 9]


def test_artifact_written(run_id):
    """6. Артефакт 04_buckets.json создан."""
    sort_verdicts(_all_twelve_verdicts(), run_id)

    path = run_dir(run_id) / "04_buckets.json"
    assert path.exists()


def test_empty_input_gives_four_empty_buckets(run_id):
    """7. Пустой вход даёт четыре пустые кучи, без падения."""
    buckets = sort_verdicts([], run_id)

    assert buckets.violated == []
    assert buckets.no_data == []
    assert buckets.checked == []
    assert buckets.failed == []
