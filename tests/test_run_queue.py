"""
Тесты очереди прогонов (app/run_queue.py; 27.10 перенесены из tests/test_web.py вместе с кодом).

Прогон подменяется поддельным: настоящий run_pipeline идёт семь минут. Текст шапки («слот 1 из 2») —
app/web.py, поэтому web тоже импортируется.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterable
from pathlib import Path

import pytest

from app import config, run_queue, trace, web

# ---- вспомогательное ----


class _BlockingRunner:
    """Поддельный прогон: занимает слот очереди, пока тест его не отпустит."""

    def __init__(self, run_ids: Iterable[str]) -> None:
        self.started = {run_id: threading.Event() for run_id in run_ids}
        self.release = {run_id: threading.Event() for run_id in run_ids}
        self.calls: list[str] = []

    def __call__(self, docx_path: Path, run_id: str) -> None:
        self.calls.append(run_id)
        self.started[run_id].set()
        self.release[run_id].wait(timeout=10)


async def _wait_started(runner: _BlockingRunner, run_id: str, timeout: float = 5.0) -> None:
    """Дождаться реального старта прогона в потоке, не блокируя событийный цикл."""
    deadline = time.monotonic() + timeout
    while not runner.started[run_id].is_set():
        assert time.monotonic() < deadline, f"прогон {run_id} так и не стартовал"
        await asyncio.sleep(0.01)


async def _wait_released(queue: run_queue.RunQueue, run_id: str, timeout: float = 5.0) -> None:
    """Дождаться, пока прогон покинет очередь (слот освободился)."""
    deadline = time.monotonic() + timeout
    while queue.state(run_id).status != "неизвестен":
        assert time.monotonic() < deadline, f"прогон {run_id} не завершился"
        await asyncio.sleep(0.01)


async def _finish_all(queue: run_queue.RunQueue, runner: _BlockingRunner) -> None:
    """Отпустить все прогоны и дождаться опустошения очереди — чтобы цикл закрылся чисто."""
    for event in runner.release.values():
        event.set()
    for run_id in runner.started:
        await _wait_released(queue, run_id)
    await asyncio.sleep(0.05)


# ---- 2-4. очередь прогонов ----


def test_third_run_waits_in_queue_and_sees_its_position(monkeypatch):
    monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 2)

    async def scenario() -> None:
        runner = _BlockingRunner(["r1", "r2", "r3", "r4"])
        queue = run_queue.RunQueue(runner)
        for run_id in ("r1", "r2", "r3", "r4"):
            queue.submit(run_id, Path(f"/tmp/{run_id}.docx"))

        await _wait_started(runner, "r1")
        await _wait_started(runner, "r2")

        assert queue.state("r1").status == "идёт"
        assert queue.state("r1").slot == 1
        assert queue.state("r2").slot == 2

        third = queue.state("r3")
        assert third.status == "в очереди"
        assert third.ahead == 0
        fourth = queue.state("r4")
        assert fourth.status == "в очереди"
        assert fourth.ahead == 1
        assert web.queue_text(fourth, "в очереди") == "в очереди, перед вами 1"
        assert web.queue_text(queue.state("r1"), "идёт") == "слот 1 из 2"
        assert not runner.started["r3"].is_set()

        await _finish_all(queue, runner)

    asyncio.run(scenario())


def test_queue_writes_service_journal(monkeypatch):
    """Журнал сервиса (2.7): прогон поставлен в очередь, стартовал в слоте, закончился."""
    monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 1)

    async def scenario() -> None:
        runner = _BlockingRunner(["r1", "r2"])
        queue = run_queue.RunQueue(runner)
        queue.submit("r1", Path("/tmp/r1.docx"))
        queue.submit("r2", Path("/tmp/r2.docx"))
        await _wait_started(runner, "r1")
        await _finish_all(queue, runner)

    asyncio.run(scenario())
    events = [(e["event"], e["run_id"]) for e in trace.read_service_events() if "run_id" in e]
    assert events[:3] == [("run_queued", "r1"), ("run_started", "r1"), ("run_queued", "r2")]
    assert ("run_finished", "r1") in events and ("run_started", "r2") in events and events[-1] == ("run_finished", "r2")
    started = [e for e in trace.read_service_events() if e["event"] == "run_started"]
    assert started[0]["slot"] == 1 and started[0]["waited_s"] >= 0


def test_queue_moves_on_by_itself_when_slot_frees(monkeypatch):
    monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 2)

    async def scenario() -> None:
        runner = _BlockingRunner(["r1", "r2", "r3"])
        queue = run_queue.RunQueue(runner)
        for run_id in ("r1", "r2", "r3"):
            queue.submit(run_id, Path(f"/tmp/{run_id}.docx"))

        await _wait_started(runner, "r1")
        await _wait_started(runner, "r2")
        assert queue.state("r3").status == "в очереди"

        # Слот освободился — третий прогон стартует сам, без действий пользователя
        runner.release["r1"].set()
        await _wait_started(runner, "r3")

        assert queue.state("r3").status == "идёт"
        assert runner.calls[:2] == ["r1", "r2"]
        assert "r3" in runner.calls

        await _finish_all(queue, runner)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("limit", "expected_status"),
    [(1, "в очереди"), (2, "идёт")],
)
def test_concurrency_limit_is_taken_from_config(monkeypatch, limit: int, expected_status: str):
    monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", limit)

    async def scenario() -> None:
        runner = _BlockingRunner(["r1", "r2"])
        queue = run_queue.RunQueue(runner)
        queue.submit("r1", Path("/tmp/r1.docx"))
        queue.submit("r2", Path("/tmp/r2.docx"))

        await _wait_started(runner, "r1")
        if expected_status == "идёт":
            await _wait_started(runner, "r2")

        assert queue.state("r2").status == expected_status
        assert queue.state("r1").limit == limit

        await _finish_all(queue, runner)

    asyncio.run(scenario())


# ---- 8. фоновый прогон не блокирует событийный цикл ----


def test_run_goes_to_background_and_leaves_event_loop_free(monkeypatch):
    monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 2)
    started = threading.Event()

    def slow_runner(docx_path: Path, run_id: str) -> None:
        """Синхронный прогон, как настоящий run_pipeline — только полсекунды."""
        started.set()
        time.sleep(0.5)

    async def scenario() -> None:
        queue = run_queue.RunQueue(slow_runner)
        queue.submit("r1", Path("/tmp/r1.docx"))

        ticks = 0
        deadline = time.monotonic() + 5
        while queue.state("r1").status != "неизвестен":
            assert time.monotonic() < deadline, "прогон не завершился"
            await asyncio.sleep(0.01)
            ticks += 1

        assert started.is_set()
        # Цикл крутился всё время прогона: будь вызов синхронным, тик был бы один
        assert ticks > 20

    asyncio.run(scenario())
