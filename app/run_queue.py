"""
Очередь прогонов сервиса П-10 (27.10, модульность: вынесена из app/web.py без изменений).

Одновременно исполняется не больше config.MAX_CONCURRENT_RUNS прогонов (константа в конфиге, меняется без правки
кода), остальные ждут и стартуют сами при освобождении слота. Сам run_pipeline — синхронная функция на семь минут,
поэтому исполняется в отдельном потоке через nicegui.run.io_bound: событийный цикл остаётся свободным и страница
не встаёт. Очередь живёт в памяти процесса: перезапуск сервиса её теряет — поэтому перезапуск только без активных
прогонов (прогон, оборванный на середине, всё равно не продолжить).

Готовые очереди (Celery, RQ, Huey, arq) не взяты: им нужен брокер и отдельный процесс-исполнитель, а экран
показывает «слот 1 из 2» и «перед вами 1» — этого не умеют ни они, ни семафор (план модульности, П1).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from nicegui import run as nicegui_run

from app import config, trace
from app.logger import get_run_logger
from app.pipeline import run_pipeline
from app.trace import read_progress

# Сигнатура функции прогона: путь к акту и id прогона (подменяется в тестах)
Runner = Callable[[Path, str], object]


@dataclass(frozen=True)
class QueueState:
    """Положение прогона в очереди: идёт в слоте, ждёт своей очереди или неизвестен."""

    status: Literal["идёт", "в очереди", "неизвестен"]
    slot: int | None = None
    ahead: int | None = None
    limit: int = 0


@dataclass(frozen=True)
class _Job:
    """Заявка на прогон: что считать и в какой папке."""

    run_id: str
    docx_path: Path
    queued_at: float = field(default_factory=time.monotonic)   # для журнала сервиса: сколько ждал слота


class RunQueue:
    """
    Очередь прогонов: одновременно не больше config.MAX_CONCURRENT_RUNS.

    Лимит читается из конфига в момент решения, а не запоминается при создании
    очереди: владелец меняет константу — поведение меняется без правки страницы.

    Лока здесь нет намеренно: все методы вызываются из событийного цикла (нажатие
    кнопки, завершение задачи), а сам семиминутный прогон уходит в отдельный поток
    через nicegui.run.io_bound — цикл при этом свободен и страница не встаёт.
    """

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner: Runner = runner if runner is not None else run_pipeline
        self._waiting: list[_Job] = []
        self._running: list[str] = []  # порядок = номера занятых слотов
        self._tasks: set[asyncio.Task[None]] = set()

    def submit(self, run_id: str, docx_path: Path) -> None:
        """Поставить прогон в очередь; если слот свободен — он стартует сразу."""
        self._waiting.append(_Job(run_id, docx_path))
        trace.log_service_event("run_queued", run_id=run_id, ahead=len(self._waiting) - 1)
        self._start_waiting()

    def state(self, run_id: str) -> QueueState:
        """Где прогон сейчас — это показывает шапка экрана прогона."""
        limit = config.MAX_CONCURRENT_RUNS
        if run_id in self._running:
            return QueueState("идёт", slot=self._running.index(run_id) + 1, limit=limit)
        for position, job in enumerate(self._waiting):
            if job.run_id == run_id:
                return QueueState("в очереди", ahead=position, limit=limit)
        return QueueState("неизвестен", limit=limit)

    def _start_waiting(self) -> None:
        """Запустить столько ждущих прогонов, сколько сейчас свободно слотов."""
        while self._waiting and len(self._running) < config.MAX_CONCURRENT_RUNS:
            job = self._waiting.pop(0)
            self._running.append(job.run_id)
            trace.log_service_event("run_started", run_id=job.run_id, slot=len(self._running),
                                    waited_s=round(time.monotonic() - job.queued_at, 1))
            task = asyncio.create_task(self._execute(job))
            # Ссылку на задачу держим сами: иначе её может забрать сборщик мусора
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _execute(self, job: _Job) -> None:
        """Один прогон в отдельном потоке, затем освобождение слота для следующего."""
        started = time.monotonic()
        error_text = None
        try:
            await nicegui_run.io_bound(self._runner, job.docx_path, job.run_id)
        except Exception as error:
            # Свои сбои прогон пишет в progress.json и run.log сам; сюда попадает
            # только то, что он не поймал — это в лог прогона, страницу не роняем
            get_run_logger(job.run_id).getChild("web").exception("прогон упал: %s", error)
            error_text = f"{type(error).__name__}: {error}"
        finally:
            progress = read_progress(job.run_id)
            trace.log_service_event("run_finished", run_id=job.run_id,
                                    status=progress.status if progress is not None else None,
                                    duration_s=round(time.monotonic() - started, 1), error=error_text)
            self._running.remove(job.run_id)
            self._start_waiting()


# Единственная очередь сервиса: одна на процесс, общая для всех вкладок
QUEUE = RunQueue()
