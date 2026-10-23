"""
Веб-страница сервиса П-10 — NiceGUI поверх FastAPI, порт 8030.

Четыре экрана и страница входа:

    /login          вход по логину и паролю
    /               загрузка акта (.docx, .pdf, .doc) и запуск прогона
    /run/<run_id>   граф выполнения по ходу прогона, по окончании — результат
    /history        последние прогоны, клик открывает результат старого прогона

ВХОД ПО ПАРОЛЮ. Страницу выставляют в открытый интернет через Tailscale Funnel,
поэтому она закрыта: один прогон стоит около десяти долларов и семь минут, а в
истории видны все разобранные акты. Проверка сделана ОДНИМ местом — AuthMiddleware
смотрит каждый входящий запрос и уводит незалогиненного на /login, запомнив, куда
тот шёл. Скачивание закрыто тем же местом: готовый Excel NiceGUI отдаёт по адресу
/_nicegui/auto/..., и этот префикс middleware не пропускает. Логин, пароль и ключ
подписи сессий читает app/config.py из файла .env.secrets — в коде их нет.

РАЗДЕЛЕНИЕ ЛОГИКИ И ОТРИСОВКИ. Первая половина модуля — чистые функции и класс
очереди, они ничего не знают про NiceGUI и их покрывают тесты tests/test_web.py.
Вторая половина (после разделителя «Отрисовка») — только разметка: элементы,
классы, обработчики. Между половинами — раздел «Вход по паролю»: там middleware,
которому нужны и NiceGUI, и чистые функции проверки из первой половины.

ИСТОЧНИК ПРАВДЫ — ДИСК. Состояние прогона в памяти вкладки не хранится: страница
раз в секунду перечитывает runs/<run_id>/progress.json (его пишет оркестратор при
каждом изменении состояния) и меняет только то, что изменилось. Поэтому обновление
вкладки, уход в историю и возврат обратно ничего не теряют, а прогон идёт своим
чередом независимо от того, открыт ли браузер.

ОЧЕРЕДЬ. Одновременно исполняется не больше config.MAX_CONCURRENT_RUNS прогонов
(константа в конфиге, меняется без правки этой страницы), остальные ждут и
стартуют сами при освобождении слота. Сам run_pipeline — синхронная функция на
семь минут, поэтому исполняется в отдельном потоке через nicegui.run.io_bound:
событийный цикл остаётся свободным и страница не встаёт.

ДИАЛОГ С РЕЗУЛЬТАТОМ (итерация 3). Под карточкой готового прогона — панель
«Работа с результатом»: чат с помощником (app/dialog.py), план из закрытого списка
операций с кнопками «Применить» / «Отменить», «было — стало» для шагов с агентом
(«Принять» / «Отклонить»), версии файла со скачиванием и возвратом, очередь
замечаний из заполненного экспертом Excel (app/feedback.py). Выполняет план
app/operations.py; каждое изменение — новая версия (app/versions.py). Панель, как
и граф, перечитывает диск по таймеру; правит одно окно, остальные — только смотрят.

СЕССИИ И ЖУРНАЛ ДЕЙСТВИЙ. Бета: один логин на всех, поэтому люди различаются
по сессии браузера — метке из id в cookie NiceGUI (s-7f3a91, app/actions.py). Метка
и адрес снимаются при открытии страницы; каждое действие — вход, выход, старт
прогона, открытие, скачивание, ход диалога, «Применить», «Принять», «Вернуть»,
файл эксперта — одна строка var/logs/actions.jsonl, а в чате диалога метка стоит
у реплики и у строк применённого плана. Лог сервиса — var/logs/service.log.

ИМЯ АКТА. Загруженный файл ложится в папку прогона под служебным именем
00_input.docx, и именно его видит оркестратор — в progress.json попадает
«00_input.docx». Оригинальное имя сохраняет trace.new_run() в meta.json, оттуда
его и берут шапка прогона, история и имя скачиваемого Excel.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import math
import os
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import Request
from fastapi.responses import RedirectResponse
from pydantic import TypeAdapter
from nicegui import app as nicegui_app
from nicegui import run as nicegui_run
from nicegui import ui
from nicegui.events import UiEventArguments, UploadEventArguments
from nicegui.storage import Storage
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app import config, dialog, feedback, garant_limits, garant_page, operations, sessions, trace, versions
from app.actions import Who, log_action, session_label
from app.config import ACCEPTED_EXTENSIONS, SEARCH_LIMITS, all_indicators
from app.mandatory_indicators import ACT_TYPES, NO_TYPE
from app.contracts import (
    Buckets, DataRequest, HypothesisMap, Marks, NodeState, NodeStatus, Operation, RunMeta, RunProgress, RunStatus,
)
from app.logger import app_logger, attach_service_log, get_run_logger
from app.pipeline import PROGRESS_FILE, run_pipeline

# Порт страницы (свободен, проверено)
WEB_PORT = 8030

# Как часто страница перечитывает progress.json
POLL_INTERVAL_S = 1.0

# Предел размера загружаемого акта: нормативный акт Москвы — это сотни килобайт
MAX_UPLOAD_MB = 20
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Имена файлов внутри папки прогона
# Базовое имя входного файла в папке прогона. Расширение берётся от акта:
# docling выбирает читалку по расширению, и PDF, сохранённый как .docx,
# он попытается открыть как Word и упадёт.
INPUT_STEM = "00_input"
EXCEL_FILE = "08_result.xlsx"
META_FILE = "meta.json"

# Сколько прогонов показывает экран истории
HISTORY_LIMIT = 30

# Сигнатура функции прогона: путь к акту и id прогона (подменяется в тестах)
Runner = Callable[[Path, str], object]


# ======================================================================
# Логика: форматирование
# ======================================================================


def plural(number: int, one: str, few: str, many: str) -> str:
    """Русское склонение при числе: 1 гипотеза, 2 гипотезы, 5 гипотез."""
    if 11 <= number % 100 <= 14:
        return many
    last_digit = number % 10
    if last_digit == 1:
        return one
    if 2 <= last_digit <= 4:
        return few
    return many


def format_seconds(seconds: float) -> str:
    """Длительность шага: «0,5 сек» для коротких, «139 сек» для длинных."""
    if seconds < 10:
        return f"{seconds:.1f} сек".replace(".", ",")
    return f"{round(seconds)} сек"


def format_elapsed(seconds: float) -> str:
    """Время с начала прогона: «42 сек», «4 мин 12 сек»."""
    total = int(round(seconds))
    if total < 60:
        return f"{total} сек"
    return f"{total // 60} мин {total % 60:02d} сек"


def format_cost(usd: float) -> str:
    """Накопленная стоимость прогона: «$6.40»."""
    return f"${usd:.2f}"


def format_moment(moment: datetime | None) -> str:
    """Дата и время прогона для экрана истории: «25.08.2026 08:13»."""
    return moment.strftime("%d.%m.%Y %H:%M") if moment is not None else "—"


def parse_moment(value: str | None) -> datetime | None:
    """Разобрать отметку времени из progress.json; мусор — это None, а не исключение."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def shorten(text: str, limit: int = 80) -> str:
    """Обрезать длинный текст (причину сбоя) до одной строки графа."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ======================================================================
# Логика: загрузка акта и артефакты прогона
# ======================================================================


def input_filename(act_filename: str) -> str:
    """
    Имя входного файла в папке прогона: 00_input плюс расширение исходного акта.

    Расширение обязано сохраняться — docling выбирает читалку по нему, и PDF,
    положенный под именем .docx, он попробует открыть как Word.
    """
    suffix = Path(act_filename).suffix.lower()
    if suffix not in ACCEPTED_EXTENSIONS:
        suffix = ".docx"
    return f"{INPUT_STEM}{suffix}"


# ---- 2.7 (итерация 4): тип акта на странице загрузки («Настройки прогона» убраны 23.10 — потолки по умолчанию) ----


def toggle_act_type(selected: set[str], changed: str, checked: bool) -> set[str]:
    """
    Галочки типа акта (АМ): можно несколько; «нет подходящего типа» исключает остальные
    и снимается, когда отмечен любой тип — иначе «нет типа» стоял бы рядом с «господдержкой».
    """
    if not checked:
        return selected - {changed}
    if changed == NO_TYPE:
        return {NO_TYPE}
    return (selected - {NO_TYPE}) | {changed}


def can_start(has_file: bool, act_types: set[str]) -> bool:
    """Кнопка «Проанализировать»: файл принят и выбран хотя бы один тип акта (14.10: поле регионов сравнения
    убрано — сравнение с регионами снято у индикаторов 7, 8, 11 до БЛ-3)."""
    return has_file and bool(act_types)


def validate_upload(filename: str, size: int) -> str | None:
    """
    Проверить загружаемый файл: расширение, непустоту, разумный размер.

    Возвращает текст отказа для показа аналитику или None, если файл годится.
    """
    if not filename.lower().endswith(ACCEPTED_EXTENSIONS):
        formats = ", ".join(ACCEPTED_EXTENSIONS)
        return f"Нужен файл одного из форматов {formats} — «{filename}» другого формата."
    if size <= 0:
        return f"Файл «{filename}» пустой — анализировать нечего."
    if size > MAX_UPLOAD_BYTES:
        return f"Файл «{filename}» больше {MAX_UPLOAD_MB} МБ — это не похоже на нормативный акт."
    return None


def read_progress(run_id: str) -> RunProgress | None:
    """
    Прочитать progress.json прогона. Любая неудача — None, а не исключение.

    Страница опрашивает файл раз в секунду, а его может ещё не быть (прогон стоит
    в очереди и не начинался) или он может не разобраться. Такой тик просто
    пропускается: показывать аналитику трассировку незачем.
    """
    try:
        return trace.load_json(run_id, PROGRESS_FILE, RunProgress)
    except Exception:
        return None


def read_act_filename(run_id: str, progress: RunProgress | None = None) -> str:
    """
    Оригинальное имя загруженного акта — из meta.json (см. docstring модуля).

    Откат на progress.json, а если и его нет — на id прогона: имя нужно только
    для показа, из-за него страница падать не должна.
    """
    try:
        return trace.load_json(run_id, META_FILE, RunMeta).act_filename
    except Exception:
        return progress.act_filename if progress is not None else run_id


def download_filename(act_filename: str, version: int | None = None) -> str:
    """
    Имя, под которым Excel уезжает аналитику в «Загрузки»: собрано из имени акта,
    чтобы после десяти прогонов там не лежало десять файлов result.xlsx.
    Версия диалогового режима — в имени (план итерации 3, шаг 1.5): иначе в «Загрузках»
    копятся файлы с одинаковым именем и разным содержимым.
    """
    stem = Path(act_filename).name.strip()
    for ext in ACCEPTED_EXTENSIONS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)].strip()
            break
    suffix = f", версия {version}" if version else ""
    return f"{stem} — карта гипотез{suffix}.xlsx" if stem else f"карта гипотез{suffix}.xlsx"


def excel_file(run_id: str, progress: RunProgress | None) -> Path | None:
    """
    Путь к готовому Excel прогона или None, если файла нет.

    При сбое прогона файл всё равно может существовать (узел [7] успел
    отработать), поэтому проверяются и путь из сводки, и файл в папке прогона.
    """
    if progress is not None and progress.summary is not None and progress.summary.excel_path:
        from_summary = Path(progress.summary.excel_path)
        if from_summary.is_file():
            return from_summary
    in_run_dir = trace.run_dir(run_id) / EXCEL_FILE
    return in_run_dir if in_run_dir.is_file() else None


def failed_node(progress: RunProgress) -> NodeState | None:
    """Первый сбойный узел — на каком шаге встал прогон."""
    return next((node for node in progress.nodes if node.status == "сбой"), None)


# ======================================================================
# Логика: история прогонов
# ======================================================================


@dataclass(frozen=True)
class HistoryItem:
    """Строка экрана истории."""

    run_id: str
    act_filename: str
    started_at: datetime | None
    duration_s: float | None
    status: RunStatus
    cost_usd: float


def list_history(limit: int = HISTORY_LIMIT) -> list[HistoryItem]:
    """
    Прогоны от новых к старым, читаются из runs/*/progress.json.

    Папки без progress.json пропускаются молча — это прогоны, сделанные до
    появления страницы, их несколько.
    """
    runs_root = trace.RUNS_DIR
    if not runs_root.is_dir():
        return []

    items: list[HistoryItem] = []
    for directory in runs_root.iterdir():
        if not directory.is_dir():
            continue
        progress = read_progress(directory.name)
        if progress is None:
            continue
        started = parse_moment(progress.started_at)
        finished = parse_moment(progress.finished_at)
        items.append(
            HistoryItem(
                run_id=directory.name,
                act_filename=read_act_filename(directory.name, progress),
                started_at=started,
                # Длительность известна только у законченных прогонов
                duration_s=(finished - started).total_seconds() if started and finished else None,
                status=progress.status,
                cost_usd=progress.total_cost_usd,
            )
        )

    # От новых к старым; у прогона без разобранного времени старта — конец списка
    items.sort(key=lambda item: (item.started_at or datetime.min, item.run_id), reverse=True)
    return items[:limit]


# ======================================================================
# Логика: очередь прогонов
# ======================================================================


@dataclass(frozen=True)
class QueueState:
    """Положение прогона в очереди: идёт в слоте, ждёт своей очереди или неизвестен."""

    status: Literal["идёт", "в очереди", "неизвестен"]
    slot: int | None = None
    ahead: int | None = None
    limit: int = 0


def queue_text(state: QueueState, run_status: RunStatus | None) -> str:
    """Правая часть шапки прогона: «слот 1 из 2» или «в очереди, перед вами 1»."""
    if state.status == "идёт":
        return f"слот {state.slot} из {state.limit}"
    if state.status == "в очереди":
        if state.ahead:
            return f"в очереди, перед вами {state.ahead}"
        return "в очереди, следующий на запуск"
    # Очередь про этот прогон не знает: либо он давно закончился и открыт из
    # истории, либо сервис перезапускали прямо во время прогона
    if run_status == "идёт":
        return "прогон не отслеживается — похоже, сервис перезапускали"
    return ""


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


# ======================================================================
# Логика: диалог с результатом прогона (итерация 3)
# ======================================================================

# Как часто панель диалога перечитывает чат и состояние с диска: секунды в строке
# состояния тикают вживую, второе окно видит то же самое
DIALOG_POLL_S = 1.0

# Сколько окно держит замок правки без продления: окно, пропавшее без отключения
# (сон ноутбука, обрыв сети), через это время отдаёт правку другому окну
DIALOG_LOCK_TTL_S = 15.0

DIALOG_OLD_RUN = "Диалог работает с прогонами версии 2.4 и новее — этот прогон собран по старым правилам."
DIALOG_READ_ONLY = "Этот прогон правят в другом окне — здесь только просмотр. Правка откроется, когда то окно закроют."

_OPERATION = TypeAdapter(Operation)


def session_closed_text(run_id: str) -> str | None:
    """Строка «Сессия закрыта …» для страницы; None — сессия открыта (2.7, конец сессии)."""
    if not sessions.is_closed(run_id):
        return None
    try:
        summary = json.loads((trace.run_dir(run_id) / sessions.SESSION_SUMMARY_FILE).read_text(encoding="utf-8"))
        when = format_moment(parse_moment(summary.get("closed_at")))
        by = "кнопкой «Завершить работу»" if summary.get("closed_by") == "пользователь" else "сама — простой больше часа"
    except (OSError, ValueError):
        when, by = "—", "причина не прочитана"
    return f"Сессия закрыта {when} ({by}). Напишите сообщение в чат — работа продолжится."


def dialog_unavailable(run_id: str, progress: RunProgress | None) -> str | None:
    """Почему диалог по прогону недоступен; None — доступен (завершён без сбоя, артефакты v2.4+). С 23.10 закрытая
    сессия чат не прячет: первое действие в чате открывает её заново (DialogJobs.start)."""
    if progress is None or progress.status != "готов":
        return "Диалог доступен после успешного завершения прогона."
    try:
        trace.load_json(run_id, "06_map.json", HypothesisMap)
        trace.load_json(run_id, "07_request.json", DataRequest)
        trace.load_json(run_id, "04_buckets.json", Buckets)
    except Exception:
        return DIALOG_OLD_RUN
    return None


class DialogLock:
    """
    Замок правки прогона: правит одно окно, остальные окна того же прогона —
    только просмотр. Окно-хозяин продлевает замок на каждом опросе; время
    передаётся параметром, как в LoginThrottle, — чтобы тест не спал.
    """

    def __init__(self, ttl_s: float = DIALOG_LOCK_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._held: dict[str, tuple[str, float]] = {}   # прогон → (окно, когда продлён)

    def acquire(self, run_id: str, client_id: str, now: float) -> bool:
        """Взять или продлить замок; False — прогон правит другое окно."""
        holder = self._held.get(run_id)
        if holder is None or holder[0] == client_id or now - holder[1] > self._ttl_s:
            self._held[run_id] = (client_id, now)
            return True
        return False

    def release(self, run_id: str, client_id: str) -> None:
        """Снять замок при закрытии окна — только свой."""
        if self._held.get(run_id, ("", 0.0))[0] == client_id:
            del self._held[run_id]


DIALOG_LOCK = DialogLock()


def op_title(op: Operation) -> str:
    """Шаг плана человеческими словами — для карточки плана."""
    kind = op.kind
    if kind == "правка текста":
        where = {"вердикт": f"Вердикты, индикатор {op.key}", "строка карты": f"строка карты {op.key}",
                 "позиция запроса": f"позиция запроса {op.key}"}[op.sheet]
        effect = f", эффект {op.effect_index}" if op.effect_index else ""
        return f"Поправить текст — {where}, поле «{op.field}»{effect}: «{shorten(op.new_text, 400)}»"
    if kind == "смена вердикта":
        return f"Сменить вердикт индикатора {op.indicator} на «{op.verdict}»: {shorten(op.rationale, 300)}"
    if kind == "удалить строку карты":
        return f"Удалить строку карты {op.row_id}"
    if kind == "добавить строку карты":
        indicators = ", ".join(map(str, op.indicators))
        return f"Добавить строку карты (индикаторы {indicators}): «{shorten(op.hypothesis, 300)}»"
    if kind == "слить строки карты":
        rows = ", ".join(map(str, op.row_ids))
        return f"Слить строки карты {rows} в одну: «{shorten(op.hypothesis, 300)}»"
    if kind == "пометить: нигде не ведётся":
        return f"Пометить: нигде не ведётся — строка карты {op.row_id}, цифра «{shorten(op.metric, 200)}»"
    if kind == "удалить позицию запроса":
        return f"Удалить позицию запроса {op.num}"
    if kind == "разделить позицию запроса":
        parts = "; ".join(f"«{part.metric}», {part.unit}" for part in op.parts)
        return f"Разделить позицию запроса {op.num} на {len(op.parts)}: {parts}"
    if kind == "перепроверить индикатор":
        return f"Перепроверить индикатор {op.indicator} его агентом. Уточнение задачи: {shorten(op.clarification, 300)}"
    if kind == "построить гипотезы":
        return f"Построить гипотезы по индикатору {op.indicator} агентом гипотез"
    return f"Дополнить запрос под строки карты {', '.join(map(str, op.row_ids))} агентом запроса"


def _raw_cost(run_id: str, relname: str) -> float | None:
    """Стоимость вызова из его сырого ответа (последняя запись CLI — итог с total_cost_usd)."""
    try:
        data = json.loads((trace.run_dir(run_id) / relname).read_text(encoding="utf-8"))
        return float((data[-1] if isinstance(data, list) else data)["total_cost_usd"])
    except Exception:
        return None


def estimate_cost(run_id: str, op: Operation) -> float | None:
    """
    Цена платного шага до запуска — сколько тот же агент стоил в исходном прогоне
    этого акта. None — шаг без агента или сырого ответа нет (цена будет после выполнения).
    """
    if op.kind == "перепроверить индикатор":
        return _raw_cost(run_id, f"03_verdicts/{op.indicator:02d}.raw.json")
    if op.kind == "построить гипотезы":
        folder = trace.run_dir(run_id) / "05_hypotheses"
        costs = [c for p in sorted(folder.glob("[0-9][0-9].raw.json"))
                 if (c := _raw_cost(run_id, f"05_hypotheses/{p.name}")) is not None]
        return sum(costs) / len(costs) if costs else None
    if op.kind == "дополнить запрос":
        return _raw_cost(run_id, "07_request.raw.json")
    return None


def cost_note(op: Operation, cost: float | None) -> str:
    """Подпись цены у шага плана."""
    if not operations.is_model_op(op):
        return "без агента · бесплатно"
    return "агент · цена — после выполнения" if cost is None else f"агент · ≈ {format_cost(cost)}"


def pending_plan(chat: list[dict]) -> list[Operation]:
    """План последнего ответа помощника, если после него ничего не было, — он ждёт решения."""
    if not chat or chat[-1]["role"] != "ассистент":
        return []
    return [_OPERATION.validate_python(p) for p in chat[-1].get("plan") or []]


def stale_notes(before: Marks, after: Marks) -> list[str]:
    """Что шаг пометил «устарело» — только новые пометки, по листам."""
    notes = []
    for label, old, new in (("Вердикты, инд.", before.stale_verdicts, after.stale_verdicts),
                            ("строка карты", before.stale_rows, after.stale_rows),
                            ("позиция запроса", before.stale_items, after.stale_items)):
        notes += [f"{label} {key}: {why}" for key, why in new.items() if old.get(key) != why]
    return notes


def step_text(result: operations.StepResult) -> str:
    """Итог шага для чата: версия, что изменилось, «было — стало»."""
    change = result.change
    head = f"Версия {result.version}. {change.target} — {change.action}"
    if change.before is None and change.after is None:
        return head
    before, after = change.before or "—", change.after or "—"
    if len(before) + len(after) <= 80:
        return f"{head}: «{before}» → «{after}»"
    return f"{head}\nБыло: {shorten(before, 800)}\nСтало: {shorten(after, 1500)}"


def version_rows(run_id: str) -> list[tuple[int, str]]:
    """Версии файла от новой к исходной: номер и что в ней изменилось. Пусто — диалога ещё не было."""
    state = versions.load_state(run_id)
    if state is None:
        return []
    changes: dict[int, list[str]] = {}
    for entry in versions.read_journal(run_id):
        changes.setdefault(entry.version, []).append(f"{entry.at[11:16]} · {entry.target} — {entry.action}")
    rows = [(n, "; ".join(changes.get(n, []))) for n in range(state.current, 0, -1)]
    return rows + [(0, "исходный разбор сервиса")]


def dialog_spent(chat: list[dict]) -> float:
    """Сколько потрачено в диалоге по прогону: ходы помощника и шаги с агентом."""
    return sum(message.get("cost_usd") or 0.0 for message in chat)


# Тон строки состояния: готово — можно работать; работа — идёт действие;
# решение — ждёт человека; сбой — последнее действие не удалось; просмотр — правят в другом окне
PanelTone = Literal["готово", "работа", "решение", "сбой", "просмотр"]


@dataclass(frozen=True)
class PanelState:
    """Строка состояния панели диалога: тон (цвет и значок), что происходит, что делать."""

    tone: PanelTone
    title: str
    detail: str


_URL_RE = re.compile(r"https?://[^\s<>«»\"]+")


def sources_html(sources: list[str]) -> str:
    """Источники ответа помощника (шаг 7 плана помощника) — HTML под ответом: адреса страниц — ссылками."""
    if not sources:
        return ""
    items = []
    for src in sources:
        text = html.escape(src)
        items.append(_URL_RE.sub(lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noopener" '
                                           f'class="text-primary underline">{m.group(0)}</a>', text))
    return ('<div class="text-xs text-gray-600 mt-2">Источники:<br>' + "<br>".join(f"• {i}" for i in items)
            + "</div>")


def limits_text(left: dict[str, int] | None) -> str:
    """Строка остатка лимитов Гаранта на панели; None — узнать не удалось."""
    if left is None:
        return "Гарант: остаток лимитов узнать не удалось"
    parts = [f"{title.lower() if title != garant_limits.EXPORT else 'выгрузки'} {left[title]} из {total}"
             for title, total in garant_limits.MONTHLY.items() if title in left and title != "Информация о документе"]
    return "Гарант в этом месяце: " + ", ".join(parts)


def panel_state(chat: list[dict], status: dict | None, editable: bool, version: int, now: datetime) -> PanelState:
    """
    Одно состояние панели, только из диска: сначала идущее действие (dialog/status.json),
    потом то, что ждёт человека (план), потом итог последней реплики.
    """
    if status is not None:
        if status.get("server") != dialog.SERVER_ID:
            return PanelState("сбой", "Действие прервано",
                              f"сервис перезапускался во время «{status.get('title', '')}». "
                              f"Файл — версия {version}; повторите запрос")
        if status["state"] == "решение":
            return PanelState("решение", status["title"], status["detail"])
        started = parse_moment(status.get("since"))
        elapsed = format_elapsed(max(0.0, (now - started).total_seconds())) if started else ""
        return PanelState("работа", status["title"], " · ".join(p for p in (elapsed, status["detail"]) if p))
    if not editable:
        return PanelState("просмотр", "Только просмотр", "правка идёт в другом окне")
    if pending_plan(chat):
        return PanelState("решение", "Ждёт вашего решения",
                          "примените или отмените план ниже; новое сообщение отменит план")
    if chat and chat[-1].get("mark") in ("сбой", "частично"):
        return PanelState("сбой", shorten(chat[-1]["text"].splitlines()[0], 160), f"файл — версия {version}")
    return PanelState("готово", "Готово", f"Файл — версия {version}. Можно спрашивать или давать правки")


def plan_summary(total: int, applied: int, rejected: int, failed: int | None, why: str | None,
                 version: int) -> tuple[str, str]:
    """Итоговая строка плана в чат и её отметка (готово / частично / сбой / отменено)."""
    steps = f"{applied} из {total} {plural(total, 'шага', 'шагов', 'шагов')}"
    if failed is not None:
        if applied:
            return f"План применён частично: {steps}, файл — версия {version}. Шаг {failed} не выполнен — {why}.", "частично"
        return f"План не применён: шаг {failed} не выполнен — {why}. Файл не изменён (версия {version}).", "сбой"
    if applied == 0:
        return f"План завершён без изменений — файл остался в версии {version}.", "отменено"
    tail = f", отклонено {rejected}" if rejected else ""
    return f"Готово: план применён ({steps}{tail}). Файл — версия {version}, скачать можно кнопкой вверху.", "готово"


def _current_version(run_id: str) -> int:
    state = versions.load_state(run_id)
    return state.current if state is not None else 0


class DialogJobs:
    """
    Долгие действия диалога — ход помощника, план, возврат версии — живут на сервере,
    а не в окне браузера. Что идёт сейчас, пишется в dialog/status.json: перезагрузка
    страницы и второе окно видят то же самое. Результат агента («было — стало») ждёт
    решения здесь, пока его не примут или не отклонят, — закрытое окно его не теряет.
    По одному прогону одновременно идёт одно действие.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._waiting: dict[str, tuple[str, operations.Candidate, asyncio.Future]] = {}

    def busy(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def waiting(self, run_id: str) -> tuple[str, operations.Candidate] | None:
        """Результат агента, который ждёт «Принять» / «Отклонить»: заголовок шага и кандидат."""
        item = self._waiting.get(run_id)
        return None if item is None else (item[0], item[1])

    def decide(self, run_id: str, accepted: bool) -> None:
        item = self._waiting.get(run_id)
        if item is not None and not item[2].done():
            item[2].set_result(accepted)

    def start(self, run_id: str, job: Coroutine) -> bool:
        """Запустить действие, если по прогону ничего не идёт; False — занято. Закрытая сессия открывается заново (23.10)."""
        if self.busy(run_id):
            job.close()
            return False
        sessions.reopen_session(run_id)
        self._tasks[run_id] = asyncio.get_running_loop().create_task(job)
        return True

    def cancel_plan(self, run_id: str, session: str | None = None) -> None:
        dialog.append_chat(run_id, "сервис", f"План отменён — файл не изменён (версия {_current_version(run_id)}).",
                           mark="отменено", session=session)

    async def turn(self, run_id: str, text: str, session: str | None = None) -> None:
        """Ход помощника: ответ и, может быть, план — всё пишется в чат; session — чья реплика."""
        if pending_plan(dialog.read_chat(run_id)):
            dialog.append_chat(run_id, "сервис", "План выше не применён.", mark="отменено", session=session)
        dialog.set_status(run_id, "думает", "Помощник думает…", "обычно 10–40 сек")
        try:
            await nicegui_run.io_bound(lambda: dialog.turn(run_id, text, session=session))
        except Exception as error:   # сбой вне вызова модели (диск, артефакты) — в чат, сервис живёт
            dialog.append_chat(run_id, "сервис", f"Сбой хода помощника: {error}", mark="сбой", session=session)
        finally:
            dialog.clear_status(run_id)

    async def apply_plan(self, run_id: str, plan: list[Operation], session: str | None = None) -> None:
        """Шаги плана по одному; итог каждого и всего плана — в чат с меткой сессии; сбой останавливает остальные."""
        total = len(plan)
        applied = rejected = 0
        failed: int | None = None
        why: str | None = None
        dialog.append_chat(run_id, "сервис", f"Применяю план: {total} {plural(total, 'шаг', 'шага', 'шагов')}.",
                           session=session)
        try:
            for number, op in enumerate(plan, 1):
                title = f"Шаг {number} из {total}: {op_title(op)}"
                slow = operations.is_model_op(op)
                dialog.set_status(run_id, "шаг", shorten(f"Выполняю шаг {number} из {total}: {op_title(op)}", 160),
                                  "агент работает, обычно 1–5 мин" if slow else "обычно несколько секунд")
                before = versions.load_state(run_id)
                try:
                    result = await nicegui_run.io_bound(operations.run_step, run_id, op)
                    if result.candidate is not None:
                        if not await self._decision(run_id, title, result.candidate):
                            rejected += 1
                            dialog.append_chat(run_id, "сервис", f"Шаг {number} отклонён — файл не изменён.",
                                               mark="отменено", cost_usd=round(result.cost_usd, 4), session=session)
                            continue
                        dialog.set_status(run_id, "шаг", f"Записываю шаг {number} из {total}", "обычно несколько секунд")
                        result = await nicegui_run.io_bound(operations.accept, run_id, result.candidate)
                except Exception as error:   # OperationError — понятная причина; остальное — сбой, корень уже откатан
                    failed, why = number, (str(error) if isinstance(error, operations.OperationError) else f"сбой: {error}")
                    break
                applied += 1
                after = versions.load_state(run_id)
                notes = stale_notes(before.marks if before is not None else Marks(),
                                    after.marks if after is not None else Marks())
                text = step_text(result) + ("\nУстарело: " + "; ".join(notes) if notes else "")
                dialog.append_chat(run_id, "сервис", text, mark="шаг", version=result.version,
                                   cost_usd=round(result.cost_usd, 4), session=session)
        finally:
            dialog.clear_status(run_id)
        text, mark = plan_summary(total, applied, rejected, failed, why, _current_version(run_id))
        dialog.append_chat(run_id, "сервис", text, mark=mark, session=session)

    async def _decision(self, run_id: str, title: str, candidate: operations.Candidate) -> bool:
        """Ждать «Принять» / «Отклонить» по результату агента."""
        dialog.set_status(run_id, "решение", "Ждёт вашего решения",
                          "проверьте «было — стало» ниже и нажмите «Принять» или «Отклонить»")
        future = asyncio.get_running_loop().create_future()
        self._waiting[run_id] = (title, candidate, future)
        try:
            return await future
        finally:
            self._waiting.pop(run_id, None)

    async def rollback(self, run_id: str, n: int, session: str | None = None) -> None:
        """Вернуть версию n — новой версией, ничего не стирая."""
        dialog.set_status(run_id, "шаг", f"Возвращаю версию {n}", "обычно несколько секунд")
        try:
            result = await nicegui_run.io_bound(operations.rollback, run_id, n)
            dialog.append_chat(run_id, "сервис", f"Готово: {step_text(result)}", mark="готово", version=result.version,
                               session=session)
        except Exception as error:
            dialog.append_chat(run_id, "сервис", f"Вернуть версию {n} не получилось: {error}", mark="сбой",
                               session=session)
        finally:
            dialog.clear_status(run_id)


DIALOG_JOBS = DialogJobs()


# ---- Конец сессии (2.7, мониторинг): кнопка и закрытие по простою ----

def close_by_user(who: Who, run_id: str, jobs: DialogJobs = DIALOG_JOBS) -> str | None:
    """Кнопка «Завершить работу»: None — сессия закрыта; иначе — почему сейчас нельзя."""
    if sessions.is_closed(run_id):
        return "Сессия уже закрыта."
    progress = read_progress(run_id)
    if progress is None or progress.status not in ("готов", "сбой"):
        return "Разбор ещё идёт — завершить работу можно после него."
    if jobs.busy(run_id):
        return "Идёт действие в чате — дождитесь его конца."
    log_action(who, "завершить работу", run_id)
    sessions.close_session(run_id, by="пользователь")
    return None


def close_idle_sessions(now: datetime, jobs: DialogJobs = DIALOG_JOBS) -> list[str]:
    """Закрыть сессии без событий дольше SESSION_IDLE_S; по которым идёт действие диалога — не трогать."""
    closed = []
    for rid in sessions.idle_sessions(now):
        if jobs.busy(rid):
            continue
        if sessions.close_session(rid, by="таймер", now=now):
            app_logger.info("сессия %s закрыта по простою", rid)
            closed.append(rid)
    return closed


_IDLE_CHECK_S = 60   # как часто проверять простой сессий
_idle_task: asyncio.Task | None = None   # ссылка на фоновую задачу — иначе её может собрать сборщик мусора


async def _close_idle_loop() -> None:
    """Фоновая проверка простоя раз в минуту; сбой проверки пишется в лог сервиса и не останавливает её."""
    while True:
        await asyncio.sleep(_IDLE_CHECK_S)
        try:
            close_idle_sessions(datetime.now())
        except Exception:
            app_logger.exception("проверка простоя сессий не удалась")


def _start_idle_loop() -> None:
    global _idle_task
    _idle_task = asyncio.get_running_loop().create_task(_close_idle_loop())


nicegui_app.on_startup(_start_idle_loop)

# ======================================================================
# Логика: вход по паролю
# ======================================================================

# Адрес страницы входа — он же адрес, на который уходит незалогиненный посетитель
LOGIN_PATH = "/login"

# Одно сообщение на любую неверную пару: по нему не понять, ошибка в логине или в
# пароле. Подсказок вроде «такого логина нет» подбирающему давать нельзя.
LOGIN_ERROR = "Неверный логин или пароль"

# Ключи в app.storage.user (пользовательская сессия NiceGUI, подписанный cookie)
SESSION_AUTHENTICATED = "authenticated"
SESSION_REFERRER = "referrer_path"

# Служебные адреса NiceGUI: скрипты, стили и сокет. Без них не отрисуется сама
# страница входа, поэтому они открыты.
NICEGUI_PREFIXES = ("/_nicegui/", "/_nicegui_ws/")

# А это NiceGUI тоже отдаёт из /_nicegui/, но открывать нельзя: сюда
# ui.download.file кладёт готовый Excel с разбором нормативного акта.
NICEGUI_FILES_PREFIX = "/_nicegui/auto/"

# Сколько промахов подряд с одного адреса прощается без задержки
LOGIN_FREE_ATTEMPTS = 3

# Насколько удлиняется пауза с каждым следующим промахом
LOGIN_DELAY_STEP_S = 5.0

# Дольше этого никого не держим: вечных блокировок в сервисе нет
LOGIN_DELAY_MAX_S = 60.0

# Столько тишины — и промахи адреса забываются сами
LOGIN_ATTEMPT_TTL_S = 900.0


def check_credentials(login: str, password: str) -> bool:
    """
    Совпала ли пара логин-пароль с заданной в .env.secrets.

    Оба поля сравниваются всегда и целиком через hmac.compare_digest: результат —
    один bool, по нему нельзя понять, что именно не сошлось, а время ответа не
    зависит от того, сколько символов пароля угадано.

    Доступы не заданы — не совпадает ничто: сервис не должен пускать внутрь, если
    настройки почему-то пусты.
    """
    expected_login = config.auth_user()
    expected_password = config.auth_password()
    if not expected_login or not expected_password:
        return False
    login_ok = hmac.compare_digest(login.encode("utf-8"), expected_login.encode("utf-8"))
    password_ok = hmac.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8"))
    # Оба сравнения уже посчитаны — «and» здесь ничего не сокращает
    return login_ok and password_ok


def is_public_path(path: str) -> bool:
    """
    Открыт ли адрес без входа.

    Открыты ровно два вида адресов: страница входа и служебные файлы NiceGUI, без
    которых форма входа не отрисовалась бы. Готовые файлы прогона лежат тоже под
    /_nicegui/, но закрыты: Excel с разбором акта не должен уезжать по прямой ссылке.
    """
    if path == LOGIN_PATH:
        return True
    if path.startswith(NICEGUI_FILES_PREFIX):
        return False
    return path.startswith(NICEGUI_PREFIXES)


def safe_referrer(path: object) -> str:
    """
    Куда вернуть человека после входа: туда, куда он шёл, или на главную.

    Чужие адреса отсекаются: путь обязан начинаться с одной косой черты — «//сайт»
    и «http://сайт» увели бы прямо со страницы входа на чужой ресурс. Возврат на
    саму страницу входа смысла не имеет, это была бы петля.
    """
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return "/"
    if path == LOGIN_PATH:
        return "/"
    return path


def throttle_message(seconds: float) -> str:
    """Что видит попавший под задержку: сколько ещё ждать до следующей попытки."""
    return f"Слишком много неудачных попыток. Попробуйте через {max(1, math.ceil(seconds))} сек."


class LoginThrottle:
    """
    Задержка после неудачных попыток входа — чтобы подбор пароля стал бессмысленным.

    Всё живёт в памяти процесса: адрес -> (промахов подряд, когда был последний).
    Ни базы, ни вечных блокировок. Первые LOGIN_FREE_ATTEMPTS промахов прощаются
    (человек ошибся раскладкой), дальше пауза растёт на LOGIN_DELAY_STEP_S за
    промах и упирается в LOGIN_DELAY_MAX_S. Удачный вход обнуляет счётчик сразу,
    а адрес, о котором давно ничего не слышно, забывается сам.

    Время передаётся снаружи (time.monotonic()), чтобы поведение можно было
    проверить тестами, не выжидая минуту.
    """

    def __init__(self) -> None:
        self._failures: dict[str, tuple[int, float]] = {}

    def retry_after(self, address: str, now: float) -> float:
        """Сколько секунд адресу ещё ждать; 0 — можно пробовать прямо сейчас."""
        count, last = self._failures.get(address, (0, 0.0))
        if count == 0 or now - last > LOGIN_ATTEMPT_TTL_S:
            return 0.0
        if count <= LOGIN_FREE_ATTEMPTS:
            return 0.0
        delay = min((count - LOGIN_FREE_ATTEMPTS) * LOGIN_DELAY_STEP_S, LOGIN_DELAY_MAX_S)
        return max(0.0, delay - (now - last))

    def register_failure(self, address: str, now: float) -> None:
        """Записать промах: счётчик растёт, отсчёт паузы начинается заново."""
        count, last = self._failures.get(address, (0, 0.0))
        if count and now - last > LOGIN_ATTEMPT_TTL_S:
            count = 0  # прошлая серия давно кончилась, эта начинается с чистого листа
        self._failures[address] = (count + 1, now)
        self._forget_stale(now)

    def register_success(self, address: str) -> None:
        """Удачный вход — промахи адреса забываются немедленно."""
        self._failures.pop(address, None)

    def _forget_stale(self, now: float) -> None:
        """Выкинуть адреса, о которых давно ничего не слышно: словарь не должен расти вечно."""
        for address, (_, last) in list(self._failures.items()):
            if now - last > LOGIN_ATTEMPT_TTL_S:
                del self._failures[address]


# Единственный счётчик попыток на процесс — общий для всех вкладок, как и очередь
LOGIN_THROTTLE = LoginThrottle()


# ======================================================================
# Вход по паролю: проверка на каждый запрос
# ======================================================================


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Единственное место, где проверяется вход, — и оно накрывает весь сервис.

    Middleware стоит перед маршрутизацией, поэтому под проверку попадает всё:
    страницы, скачивание Excel, любой экран, который появится позже. Копипасты по
    страницам нет, и забыть закрыть новую страницу невозможно.

    Порядок middleware важен: этот регистрируется при импорте модуля, а
    SessionMiddleware и RequestTrackingMiddleware NiceGUI добавляет позже, в
    ui.run(). Добавленный позже оказывается снаружи — значит к моменту нашей
    проверки сессия уже разобрана и app.storage.user доступен.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Пропустить запрос дальше или увести на страницу входа."""
        path = request.url.path
        if is_public_path(path) or nicegui_app.storage.user.get(SESSION_AUTHENTICATED, False):
            return await call_next(request)
        if not path.startswith(NICEGUI_PREFIXES):
            # Запомнить, куда человек шёл: после входа вернём именно туда, а не на
            # главную. Служебные адреса не запоминаем — возвращать на них нечего
            nicegui_app.storage.user[SESSION_REFERRER] = path
        return RedirectResponse(LOGIN_PATH)


# Регистрируется при импорте модуля: после старта FastAPI middleware добавить нельзя
nicegui_app.add_middleware(AuthMiddleware)


# ======================================================================
# Отрисовка
# ======================================================================

# Цвет статуса прогона на экране истории
_STATUS_CLASS: dict[str, str] = {
    "готов": "text-green-700",
    "сбой": "text-red-700",
    "идёт": "text-blue-700",
    "в очереди": "text-gray-500",
}

# Пять чисел итоговой сводки: поле RunSummary -> подпись
_SUMMARY_COUNTERS: tuple[tuple[str, str], ...] = (
    ("violated", "нарушено"),
    ("compliant", "соблюдено"),
    ("not_applicable", "не применимо"),
    ("no_data", "нет данных"),
    ("errors", "ошибок"),
)


def _who(request: Request) -> Who:
    """Кто открыл страницу: метка сессии браузера (id из cookie NiceGUI) и адрес — снимаются при открытии."""
    return Who(session=session_label(nicegui_app.storage.browser.get("id")),
               address=request.client.host if request.client is not None else None,
               forwarded=request.headers.get("x-forwarded-for"))


def _download(who: Who, run_id: str, path: Path, act: str, version: int | None) -> None:
    """Скачивание Excel — с записью в журнал действий."""
    name = download_filename(act, version)
    log_action(who, "скачивание Excel", run_id, version=version or 0, file=name)
    ui.download.file(path, name)


def _logout(who: Who) -> None:
    """Выход: сессия чистится целиком, дальше — только через страницу входа."""
    log_action(who, "выход")
    nicegui_app.storage.user.clear()
    ui.navigate.to(LOGIN_PATH)


def _layout(who: Who) -> None:
    """
    Общая шапка и меню слева (23.10, платформа): раздел «Анализ ОРВ» — прогоны по 12 индикаторам,
    раздел «Гарант-помощник» — беседы с агентом по Гаранту (app/garant_page.py); выход — в шапке.
    """
    ui.page_title("Платформа ОРВ · П-10")
    with ui.header().classes("items-center justify-between px-6 py-3"):
        with ui.row().classes("items-center gap-2"):
            ui.button(icon="menu", on_click=lambda: drawer.toggle()).props("flat dense round color=white")
            ui.label("Платформа ОРВ · модуль П-10").classes("text-lg font-medium")
        ui.button("Выйти", on_click=lambda: _logout(who)).props("flat dense color=white")
    with ui.left_drawer(value=True).classes("bg-gray-50 px-4 py-6 gap-1") as drawer:
        # Разделы платформы: у каждого свои страницы
        ui.label("Анализ ОРВ").classes("text-xs uppercase tracking-wide text-gray-500 font-medium")
        ui.link("Новый прогон", "/").classes("text-sm no-underline text-gray-900 py-1")
        ui.link("История", "/history").classes("text-sm no-underline text-gray-900 py-1")
        ui.separator().classes("my-3")
        ui.label("Гарант-помощник").classes("text-xs uppercase tracking-wide text-gray-500 font-medium")
        ui.link("Беседы", "/garant").classes("text-sm no-underline text-gray-900 py-1")


# Раздел «Гарант-помощник» (23.10): страницы /garant и /garant/<беседа> — app/garant_page.py, общие шапка и вход
garant_page.register(_layout, _who)


def _status_icon(status: NodeStatus) -> None:
    """Значок состояния: точка ожидания, крутящийся индикатор, галочка, крест."""
    if status == "идёт":
        ui.spinner(size="sm", color="primary")
    elif status == "готов":
        ui.icon("check_circle", color="positive").classes("text-xl")
    elif status == "сбой":
        ui.icon("cancel", color="negative").classes("text-xl")
    else:
        ui.icon("fiber_manual_record", color="grey-5").classes("text-xs")


def _node_detail(node: NodeState) -> str:
    """Правая подпись шага: время выполнения, «идёт» или причина сбоя."""
    if node.status == "готов":
        return format_seconds(node.duration_s) if node.duration_s is not None else ""
    if node.status == "идёт":
        return "идёт"
    if node.status == "сбой":
        return shorten(node.error or "сбой")
    return ""


class _RunView:
    """
    Экран прогона: разметка строится один раз, а на каждом тике меняется только
    то, что действительно изменилось — иначе страница мигала бы раз в секунду.

    Ключи в _icons: ключ узла («verdicts»), «ind:<номер>» для индикатора,
    «br:<ветка>» для ветки гипотез.
    """

    def __init__(self, run_id: str, who: Who) -> None:
        self._run_id = run_id
        self._who = who
        self._built = False
        self._finished = False
        self._shown: dict[str, NodeStatus] = {}  # последнее отрисованное состояние
        self._icons: dict[str, ui.element] = {}
        self._titles: dict[str, ui.label] = {}
        self._details: dict[str, ui.label] = {}
        self._counter: ui.label | None = None

        with ui.column().classes("w-full items-center p-8 gap-4"):
            with ui.card().classes("w-full max-w-3xl p-0"):
                with ui.column().classes("w-full p-5 gap-1"):
                    self._act = ui.label(read_act_filename(run_id)).classes("text-lg font-medium")
                    self._subtitle = ui.label("ожидает запуска").classes("text-sm text-gray-600")
                ui.separator()
                self._graph = ui.column().classes("w-full p-5 gap-0")
            self._result = ui.column().classes("w-full max-w-3xl gap-4")

        self._timer = ui.timer(POLL_INTERVAL_S, self.tick)
        self.tick()  # первый опрос сразу, чтобы не смотреть секунду на пустой экран

    # ---- опрос ----

    def tick(self) -> None:
        """Один опрос progress.json — раз в секунду."""
        progress = read_progress(self._run_id)
        state = QUEUE.state(self._run_id)

        if progress is None:
            # Файла ещё нет: прогон стоит в очереди и пока не начинался
            self._set_text(self._subtitle, queue_text(state, None) or "ожидает запуска")
            return

        if not self._built:
            self._build_graph(progress)
            self._built = True

        self._update_header(progress, state)
        self._update_graph(progress)

        if progress.status in ("готов", "сбой") and not self._finished:
            self._finished = True
            self._timer.deactivate()
            self._show_result(progress)

    # ---- построение разметки (один раз) ----

    def _build_graph(self, progress: RunProgress) -> None:
        """Собрать граф выполнения: шаги, вертикальная линия, вложенные блоки."""
        with self._graph:
            for position, node in enumerate(progress.nodes):
                if position:
                    # Вертикальная линия между шагами — чтобы читалось как последовательность
                    ui.element("div").classes("w-px h-5 bg-gray-300 ml-3")
                with ui.row().classes("w-full items-center flex-nowrap gap-3 py-1"):
                    self._icons[node.key] = ui.element("div").classes("w-6 flex justify-center")
                    self._titles[node.key] = ui.label(node.title).classes("text-base")
                    ui.space()
                    self._details[node.key] = ui.label("").classes("text-sm text-gray-600 text-right")
                if node.key == "verdicts":
                    self._build_indicators()
                elif node.key == "hypotheses":
                    self._build_branches()

    def _sub_block(self) -> ui.column:
        """Вложенный блок под шагом: слева продолжается линия, справа содержимое."""
        with ui.row().classes("w-full items-stretch flex-nowrap gap-3"):
            ui.element("div").classes("w-px bg-gray-300 ml-3")
            return ui.column().classes("gap-2 py-2")

    def _build_indicators(self) -> None:
        """
        Двенадцать индикаторов поимённо — по шесть в строке, у каждого свой значок,
        плюс счётчик готовых: это пять минут ожидания, движение должно быть видно.
        """
        indicators = all_indicators()
        with self._sub_block():
            for first in (0, 6):
                with ui.row().classes("flex-nowrap gap-4"):
                    for indicator in indicators[first : first + 6]:
                        with ui.row().classes("items-center flex-nowrap gap-1") as chip:
                            self._icons[f"ind:{indicator.number}"] = (
                                ui.element("div").classes("w-4 flex justify-center")
                            )
                            ui.label(str(indicator.number)).classes("text-sm text-gray-700 w-5")
                        chip.tooltip(f"{indicator.number}. {indicator.name}")
            self._counter = ui.label("").classes("text-sm text-gray-600")

    def _build_branches(self) -> None:
        """Две ветки гипотез отдельно — видно, что считаются одновременно."""
        with self._sub_block():
            with ui.row().classes("flex-nowrap gap-6"):
                for branch, title in (("violated", "ветка нарушений"), ("no_data", "ветка пробелов")):
                    with ui.row().classes("items-center flex-nowrap gap-2"):
                        self._icons[f"br:{branch}"] = ui.element("div").classes("w-4 flex justify-center")
                        ui.label(title).classes("text-sm text-gray-700")

    # ---- обновление изменившегося ----

    def _update_header(self, progress: RunProgress, state: QueueState) -> None:
        """Шапка: статус, время с начала прогона, стоимость, занятый слот."""
        started = parse_moment(progress.started_at)
        finished = parse_moment(progress.finished_at)
        # max(0) — на случай подвода часов назад посреди семиминутного прогона
        elapsed = max(0.0, ((finished or datetime.now()) - started).total_seconds()) if started else 0.0

        parts = [f"{progress.status} {format_elapsed(elapsed)}", format_cost(progress.total_cost_usd)]
        slot = queue_text(state, progress.status)
        if slot:
            parts.append(slot)
        self._set_text(self._subtitle, " · ".join(parts))

    def _update_graph(self, progress: RunProgress) -> None:
        """Пройтись по состояниям и обновить только изменившееся."""
        for node in progress.nodes:
            self._apply(node.key, node.status)
            self._set_text(self._details[node.key], _node_detail(node))

        for number, status in progress.indicators.items():
            self._apply(f"ind:{number}", status)
        if self._counter is not None:
            done = sum(1 for status in progress.indicators.values() if status == "готов")
            self._set_text(self._counter, f"{done} из {len(progress.indicators)} готово")

        for branch, status in progress.hypotheses_branches.items():
            self._apply(f"br:{branch}", status)

    def _apply(self, key: str, status: NodeStatus) -> None:
        """Перерисовать значок шага, только если его состояние изменилось."""
        if self._shown.get(key) == status:
            return
        self._shown[key] = status

        container = self._icons.get(key)
        if container is None:
            return
        container.clear()
        with container:
            _status_icon(status)

        title = self._titles.get(key)
        if title is not None:
            # Ожидающий шаг — приглушённым текстом, начавшийся — обычным
            title.classes(replace="text-base text-gray-400" if status == "ожидает" else "text-base")

    @staticmethod
    def _set_text(label: ui.label, text: str) -> None:
        """Записать подпись, только если она изменилась (лишний обмен ни к чему)."""
        if label.text != text:
            label.set_text(text)

    # ---- результат ----

    def _show_result(self, progress: RunProgress) -> None:
        """
        Экран результата: карточка сводки и скачивания, под ней — диалог с результатом
        (итерация 3). Карточка перерисовывается после каждой новой версии файла.
        """
        with self._result:
            self._card_box = ui.column().classes("w-full")
        self._draw_result(progress)
        reason = dialog_unavailable(self._run_id, progress)
        with self._result:
            if reason is None:
                _DialogView(self._run_id, on_version=self._refresh_result, who=self._who)
            elif progress.status == "готов":
                ui.label(reason).classes("text-sm text-gray-600")

    def _refresh_result(self) -> None:
        """Новая версия файла: сводка и кнопка скачивания — по текущей версии."""
        progress = read_progress(self._run_id)
        if progress is not None:
            self._draw_result(progress)

    def _draw_result(self, progress: RunProgress) -> None:
        """Карточка результата: сводка прогона и кнопка скачивания Excel текущей версии."""
        started = parse_moment(progress.started_at)
        finished = parse_moment(progress.finished_at)
        elapsed = (finished - started).total_seconds() if started and finished else 0.0
        excel = excel_file(self._run_id, progress)
        act = read_act_filename(self._run_id, progress)
        is_ok = progress.status == "готов"

        self._card_box.clear()
        with self._card_box, ui.card().classes("w-full p-0"):
            with ui.row().classes("w-full items-center flex-nowrap gap-3 p-5"):
                if is_ok:
                    ui.icon("check_circle", color="positive").classes("text-2xl")
                    headline = f"Готово за {format_elapsed(elapsed)}"
                else:
                    ui.icon("cancel", color="negative").classes("text-2xl")
                    headline = f"Прогон завершился сбоем через {format_elapsed(elapsed)}"
                ui.label(f"{headline} · {format_cost(progress.total_cost_usd)}").classes(
                    "text-lg font-medium"
                )
            ui.separator()

            with ui.column().classes("w-full p-5 gap-5"):
                if not is_ok:
                    node = failed_node(progress)
                    reason = node.error or "причина не записана" if node is not None else "причина не записана"
                    step = f"шаг «{node.title}»" if node is not None else "шаг не определён"
                    ui.label(f"Встал на {step}: {reason}").classes("text-sm text-red-700")

                summary = progress.summary
                if summary is not None:
                    with ui.row().classes("gap-10"):
                        for field, caption in _SUMMARY_COUNTERS:
                            with ui.column().classes("items-center gap-0"):
                                ui.label(str(getattr(summary, field))).classes("text-2xl font-medium")
                                ui.label(caption).classes("text-xs text-gray-600")
                    rows = summary.map_rows
                    items = summary.request_items
                    ui.label(
                        f"{rows} {plural(rows, 'гипотеза', 'гипотезы', 'гипотез')}"
                        f"  ·  {items} {plural(items, 'показатель', 'показателя', 'показателей')}"
                        " для запроса"
                    ).classes("text-sm text-gray-700")

                if excel is not None:
                    state = versions.load_state(self._run_id)
                    version = state.current if state is not None else None
                    ui.button(
                        "Скачать Excel" + (f" · версия {version}" if version else ""),
                        icon="download",
                        on_click=lambda: _download(self._who, self._run_id, excel, act, version),
                    ).props("unelevated")
                else:
                    ui.label("Файл Excel не собран — скачивать нечего.").classes(
                        "text-sm text-gray-600"
                    )

                # Конец сессии (2.7): закрыта — строка; открыта — кнопка с подтверждением
                closed = session_closed_text(self._run_id)
                if closed is not None:
                    ui.label(closed).classes("text-sm text-gray-700")
                else:
                    ui.button("Завершить работу", icon="task_alt", on_click=self._confirm_close).props("outline")

    def _confirm_close(self) -> None:
        """Подтверждение конца сессии: закрытие нельзя отменить — поэтому спрашиваем."""
        with ui.dialog() as box, ui.card():
            ui.label("Завершить работу с актом?").classes("text-lg font-medium")
            ui.label("Сессия закроется. Продолжить можно в любой момент: откройте прогон из истории и напишите "
                     "в чат. Excel останется для скачивания.")\
                .classes("text-sm text-gray-700")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Отмена", on_click=box.close).props("flat")
                ui.button("Завершить", on_click=lambda: self._close(box)).props("unelevated")
        box.open()

    def _close(self, box: ui.dialog) -> None:
        box.close()
        error = close_by_user(self._who, self._run_id)
        if error is not None:
            ui.notify(error, type="warning")
            return
        ui.navigate.reload()   # страница заново: со строкой «Сессия закрыта … напишите — продолжится»


class _DialogView:
    """
    Панель «Работа с результатом» (итерация 3): строка состояния, чат с помощником,
    план с подтверждением, «было — стало» для шагов с агентом, версии файла, очередь
    замечаний эксперта.

    Окно только рисует. Что идёт по прогону, берётся с диска (dialog/status.json,
    dialog/chat.jsonl, versions/) и из DIALOG_JOBS, где на сервере ждёт решения
    результат агента. Поэтому перезагрузка страницы ничего не теряет, а второе окно
    того же прогона видит то же самое; правит одно окно (DIALOG_LOCK).
    """

    # тон строки состояния → значок (None — крутящийся индикатор), цвет значка, фон строки
    _TONES = {
        "готово": ("check_circle", "positive", "bg-green-50"),
        "работа": (None, "primary", "bg-blue-50"),
        "решение": ("pending_actions", "warning", "bg-amber-50"),
        "сбой": ("error", "negative", "bg-red-50"),
        "просмотр": ("visibility", "grey-7", "bg-gray-100"),
    }
    # отметка служебной строки чата → значок и цвет
    _MARKS = {
        "шаг": ("check_circle", "positive"),
        "готово": ("task_alt", "positive"),
        "сбой": ("cancel", "negative"),
        "частично": ("warning", "warning"),
        "отменено": ("undo", "grey-7"),
    }
    _STATE_ROW = "w-full items-center flex-nowrap gap-3 px-4 py-3 rounded"

    def __init__(self, run_id: str, on_version: Callable[[], None], who: Who) -> None:
        self._run_id = run_id
        self._on_version = on_version
        self._who = who                              # сессия браузера — в журнал действий и строки чата
        self._client = ui.context.client
        self._client_id = self._client.id
        self._editable: bool | None = None
        self._disconnected = False                  # окно было подключено и отключилось (закрыто, перезагружается)
        self._shown_chat = -1                       # сколько реплик отрисовано
        self._shown_tone: tuple | None = None       # (тон, заголовок) строки состояния
        self._shown_pending: tuple | None = None    # от чего зависит карточка плана или решения
        self._shown_versions: tuple | None = None   # от чего зависит список версий
        self._known_version: int | None = None      # версия, по которой нарисована карточка результата
        self._shown_steps: list[str] = []           # шаги помощника, что сейчас на экране

        with ui.card().classes("w-full p-0"):
            with ui.row().classes("w-full items-center flex-nowrap gap-3 px-5 py-4"):
                ui.icon("forum", color="primary").classes("text-2xl")
                ui.label("Работа с результатом").classes("text-lg font-medium")
                ui.space()
                with ui.column().classes("gap-0 items-end"):
                    self._spent = ui.label("").classes("text-sm text-gray-600 text-right")
                    self._limits = ui.label("").classes("text-xs text-gray-500 text-right")
            ui.separator()
            with ui.column().classes("w-full p-5 gap-3"):
                ui.label(
                    "Спросите, почему такой вердикт или откуда гипотеза; попросите поправить текст; "
                    "передайте решение эксперта или загрузите его заполненный файл. Каждое изменение — "
                    "новая версия Excel, исходная остаётся."
                ).classes("text-sm text-gray-700")
                # Строка состояния — всегда одна: что происходит и что делать
                with ui.row().classes(self._STATE_ROW) as self._state_row:
                    self._state_icon = ui.element("div").classes("w-8 flex justify-center")
                    with ui.column().classes("gap-0"):
                        self._state_title = ui.label("").classes("text-base font-medium")
                        self._state_detail = ui.label("").classes("text-sm text-gray-700")
                # Окно без права правки: почему нет кнопок
                self._lock_note = ui.label(DIALOG_READ_ONLY).classes("text-sm text-gray-600")
                self._scroll = ui.scroll_area().classes("w-full h-96 border rounded")
                with self._scroll:
                    self._chat = ui.column().classes("w-full gap-1 p-2")
                # Шаги помощника, пока идёт его ход (из файла хода, по таймеру)
                self._steps = ui.column().classes("w-full gap-0 px-3")
                self._pending = ui.column().classes("w-full gap-2")
                with ui.row().classes("w-full items-end flex-nowrap gap-2"):
                    self._input = (
                        ui.textarea(placeholder="Например: «Почему индикатор 3 нарушен?» или "
                                                "«Эксперт считает, что строка карты 2 лишняя». Ctrl+Enter — отправить")
                        .props("outlined autogrow")
                        .classes("flex-grow")
                    )
                    self._send = ui.button(icon="send", on_click=self._on_send).props("unelevated")
                with ui.row().classes("w-full items-center gap-3"):
                    # Сам загрузчик спрятан — файл выбирается кнопкой, так панель не распухает
                    self._upload = (
                        ui.upload(on_upload=self._on_expert_file, auto_upload=True, max_files=1,
                                  max_file_size=MAX_UPLOAD_BYTES)
                        .props('accept=".xlsx"')
                        .classes("hidden")
                    )
                    self._upload_button = ui.button(
                        "Загрузить файл эксперта", icon="upload_file",
                        on_click=lambda: self._upload.run_method("pickFiles"),
                    ).props("outline")
                    self._next = ui.button("", icon="skip_next", on_click=self._on_next_remark).props("outline")
            ui.separator()
            with ui.expansion("Версии файла", icon="history").classes("w-full"):
                self._versions = ui.column().classes("w-full gap-2 px-2 pb-3")

        self._input.on("keydown.ctrl.enter", self._on_send)
        self._client.on_connect(lambda: setattr(self, "_disconnected", False))
        self._client.on_disconnect(self._on_disconnect)
        ui.timer(DIALOG_POLL_S, self._tick)
        ui.timer(garant_limits.CACHE_S, self._refresh_limits)
        self._tick()
        ui.timer(0.1, self._refresh_limits, once=True)

    async def _refresh_limits(self) -> None:
        """Остаток лимитов Гаранта — в фоне, не чаще раза в минуту (кэш garant_limits)."""
        left = await nicegui_run.io_bound(garant_limits.limits)
        _RunView._set_text(self._limits, limits_text(left))

    def _on_disconnect(self) -> None:
        self._disconnected = True
        DIALOG_LOCK.release(self._run_id, self._client_id)

    # ---- опрос диска ----

    def _tick(self) -> None:
        """Продлить замок и перерисовать то, что изменилось: чат, строку состояния, карточку, версии."""
        if self._disconnected:
            # Окно закрыто или перезагружается: NiceGUI держит его ещё несколько секунд, и без этой
            # проверки его таймер снова занял бы правку — новое окно ждало бы, пока замок истечёт.
            # Флаг, а не has_socket_connection: при открытии страницы сокета ещё нет, а правка
            # нужна сразу — иначе первая секунда кнопок молча не работает
            return
        rid = self._run_id
        # Сессия закрыта (кнопкой или по простою) — панель работает, над чатом строка «напишите — продолжится»
        closed = session_closed_text(rid)
        self._editable = DIALOG_LOCK.acquire(rid, self._client_id, time.monotonic())
        _RunView._set_text(self._lock_note, closed if closed is not None and self._editable else DIALOG_READ_ONLY)
        chat = dialog.read_chat(rid)
        busy = DIALOG_JOBS.busy(rid)
        waiting = DIALOG_JOBS.waiting(rid)
        current = _current_version(rid)

        if len(chat) != self._shown_chat:
            self._shown_chat = len(chat)
            self._render_chat(chat)
        self._render_steps(chat, dialog.read_status(rid))
        pending_key = (len(chat), busy, id(waiting[1]) if waiting else None, self._editable)
        if pending_key != self._shown_pending:
            self._shown_pending = pending_key
            self._render_pending(chat, busy, waiting)
        versions_key = (current, busy, self._editable)
        if versions_key != self._shown_versions:
            self._shown_versions = versions_key
            self._render_versions(current, active=self._editable and not busy)
        if self._known_version is not None and current != self._known_version:
            self._on_version()   # новая версия — карточка результата и кнопка скачивания по ней
        self._known_version = current

        self._render_state(panel_state(chat, dialog.read_status(rid), self._editable, current, datetime.now()))
        self._lock_note.set_visibility(not self._editable or closed is not None)
        self._render_controls(chat, active=self._editable and not busy)

    def _render_state(self, state: PanelState) -> None:
        if (state.tone, state.title) != self._shown_tone:
            self._shown_tone = (state.tone, state.title)
            icon, color, background = self._TONES[state.tone]
            self._state_row.classes(replace=f"{self._STATE_ROW} {background}")
            self._state_icon.clear()
            with self._state_icon:
                if icon is None:
                    ui.spinner(size="md", color=color)
                else:
                    ui.icon(icon, color=color).classes("text-2xl")
            _RunView._set_text(self._state_title, state.title)
        _RunView._set_text(self._state_detail, state.detail)   # секунды тикают каждый опрос

    def _render_steps(self, chat: list[dict], status: dict | None) -> None:
        """Пока помощник думает — его шаги по ходу (номер хода = номер последней реплики пользователя)."""
        steps = dialog.turn_steps(self._run_id, len(chat)) if status and status.get("state") == "думает" else []
        if steps == self._shown_steps:
            return
        self._shown_steps = steps
        self._steps.clear()
        with self._steps:
            for step in steps:
                ui.label(f"→ {step}").classes("text-xs text-gray-600")

    def _render_controls(self, chat: list[dict], active: bool) -> None:
        for element in (self._input, self._send, self._upload_button, self._next):
            element.set_enabled(active)
        progress = dialog.remarks_progress(self._run_id)
        left = progress is not None and progress[0] < progress[1]
        self._next.set_visibility(left)
        if left:
            _RunView._set_text(self._next, f"Следующее замечание эксперта ({progress[0] + 1} из {progress[1]})")
        _RunView._set_text(self._spent, f"потрачено в диалоге {format_cost(dialog_spent(chat))}")

    def _render_chat(self, chat: list[dict]) -> None:
        self._chat.clear()
        with self._chat:
            if not chat:
                ui.label("Здесь будет переписка с помощником.").classes("text-sm text-gray-500 p-2")
            for message in chat:
                if message["role"] == "сервис":
                    icon, color = self._MARKS.get(message.get("mark"), ("info", "grey-6"))
                    weight = " font-medium" if message.get("mark") in ("готово", "частично", "сбой") else ""
                    with ui.row().classes("w-full items-start flex-nowrap gap-2 px-3 py-2 bg-gray-100 rounded"):
                        ui.icon(icon, color=color).classes("text-lg")
                        ui.label(message["text"]).classes(f"text-sm text-gray-800 whitespace-pre-line flex-grow{weight}")
                        if message.get("session"):
                            ui.label(message["session"]).classes("text-xs text-gray-500 whitespace-nowrap")
                    continue
                mine = message["role"] == "пользователь"
                stamp = message.get("at", "")[11:16]
                if message.get("session"):
                    stamp += f" · {message['session']}"
                if message.get("cost_usd"):
                    stamp += f" · {format_cost(message['cost_usd'])}"
                ui.chat_message(
                    text=html.escape(message["text"]).replace("\n", "<br>") + sources_html(message.get("sources") or []),
                    text_html=True, name="Вы" if mine else "Помощник", sent=mine, stamp=stamp,
                ).classes("w-full")
                if message.get("steps"):   # что помощник делал в этом ходе — свёрнуто под ответом
                    with ui.expansion(f"Шаги помощника ({len(message['steps'])})").classes("w-full text-xs text-gray-600"):
                        for step in message["steps"]:
                            ui.label(f"→ {step}").classes("text-xs text-gray-600")
        self._scroll.scroll_to(percent=1.0)

    def _render_pending(self, chat: list[dict], busy: bool, waiting: tuple | None) -> None:
        """Под чатом: результат агента на решение или план последнего ответа помощника."""
        self._pending.clear()
        if waiting is not None:
            self._render_candidate(*waiting)
            return
        plan = [] if busy else pending_plan(chat)
        if not plan:
            return
        costs = [estimate_cost(self._run_id, op) for op in plan]
        with self._pending, ui.card().classes("w-full p-4 gap-2 border-l-4 border-amber-400"):
            ui.label(f"План: {len(plan)} {plural(len(plan), 'шаг', 'шага', 'шагов')}").classes("font-medium")
            for number, (op, cost) in enumerate(zip(plan, costs), 1):
                with ui.row().classes("w-full flex-nowrap items-start gap-3"):
                    ui.label(f"{number}. {op_title(op)}").classes("text-sm flex-grow whitespace-pre-line")
                    ui.label(cost_note(op, cost)).classes("text-xs text-gray-600 whitespace-nowrap")
            paid = [c for op, c in zip(plan, costs) if operations.is_model_op(op) and c is not None]
            if paid:
                ui.label(f"Платные шаги — около {format_cost(sum(paid))}; «было — стало» покажу до записи.").classes(
                    "text-xs text-gray-600"
                )
            if self._editable:
                with ui.row().classes("gap-2"):
                    ui.button("Применить", icon="check", on_click=lambda: self._apply(plan)).props("unelevated")
                    ui.button("Отменить", on_click=self._on_cancel_plan).props("flat")

    def _render_candidate(self, title: str, candidate: operations.Candidate) -> None:
        with self._pending, ui.card().classes("w-full p-4 gap-2 border-l-4 border-amber-400"):
            ui.label(title).classes("font-medium")
            ui.label(
                f"Агент отработал, стоимость {format_cost(candidate.cost_usd)}. В файл запишется только после «Принять»."
            ).classes("text-xs text-gray-600")
            with ui.row().classes("w-full flex-nowrap gap-4 items-start"):
                for caption, text in (("Было", candidate.before), ("Стало", candidate.after)):
                    with ui.column().classes("w-1/2 gap-1"):
                        ui.label(caption).classes("text-xs uppercase text-gray-500")
                        ui.label(text).classes("text-sm whitespace-pre-line")
            if self._editable:
                with ui.row().classes("gap-2"):
                    ui.button("Принять", icon="check", on_click=lambda: self._decide(True)).props("unelevated")
                    ui.button("Отклонить", on_click=lambda: self._decide(False)).props("flat")

    def _render_versions(self, current: int, active: bool) -> None:
        self._versions.clear()
        act = read_act_filename(self._run_id)
        rows = version_rows(self._run_id)
        with self._versions:
            if not rows:
                ui.label("Изменений ещё не было — файл в исходной версии.").classes("text-sm text-gray-600")
                return
            for n, what in rows:
                path = trace.run_dir(self._run_id) / versions.VERSIONS / f"v{n}" / EXCEL_FILE
                with ui.row().classes("w-full items-start flex-nowrap gap-3"):
                    ui.label(f"v{n}" + (" · текущая" if n == current else "")).classes(
                        "text-sm font-medium w-28 shrink-0"
                    )
                    ui.label(what).classes("text-sm text-gray-700 flex-grow")
                    ui.button(
                        icon="download", on_click=lambda p=path, n=n: _download(self._who, self._run_id, p, act, n)
                    ).props("flat dense").tooltip("Скачать эту версию")
                    if n != current and active:
                        ui.button("Вернуть", on_click=lambda n=n: self._rollback(n)).props("flat dense").tooltip(
                            "Сделать новую версию с содержимым этой — ничего не стирается"
                        )

    # ---- действия (сами действия — в DIALOG_JOBS, на сервере) ----

    def _start(self, job: Coroutine) -> None:
        if not self._editable or not DIALOG_JOBS.start(self._run_id, job):
            if self._editable:
                ui.notify("Уже идёт другое действие — дождитесь его конца", type="warning")
            else:
                job.close()
        self._tick()

    def _decide(self, accepted: bool) -> None:
        waiting = DIALOG_JOBS.waiting(self._run_id)
        if self._editable and waiting is not None:
            log_action(self._who, "принять" if accepted else "отклонить", self._run_id, step=waiting[0])
            DIALOG_JOBS.decide(self._run_id, accepted)
        self._tick()

    def _apply(self, plan: list[Operation]) -> None:
        if self._editable and not DIALOG_JOBS.busy(self._run_id):
            log_action(self._who, "применить план", self._run_id, steps=[op_title(op) for op in plan])
        self._start(DIALOG_JOBS.apply_plan(self._run_id, plan, self._who.session))

    def _rollback(self, n: int) -> None:
        if self._editable and not DIALOG_JOBS.busy(self._run_id):
            log_action(self._who, "вернуть версию", self._run_id, version=n)
        self._start(DIALOG_JOBS.rollback(self._run_id, n, self._who.session))

    def _on_send(self) -> None:
        text = (self._input.value or "").strip()
        if not text or not self._editable or DIALOG_JOBS.busy(self._run_id):
            return
        self._input.set_value("")
        log_action(self._who, "сообщение помощнику", self._run_id, text=shorten(text, 500))
        self._start(DIALOG_JOBS.turn(self._run_id, text, self._who.session))

    def _on_next_remark(self) -> None:
        if not self._editable or DIALOG_JOBS.busy(self._run_id):
            return
        progress = dialog.remarks_progress(self._run_id)
        remark = dialog.next_remark(self._run_id)
        if remark is not None:
            text = feedback.remark_message(remark)
            log_action(self._who, "сообщение помощнику", self._run_id, text=shorten(text, 500),
                       source=f"замечание эксперта {progress[0] + 1} из {progress[1]}")
            self._start(DIALOG_JOBS.turn(self._run_id, text, self._who.session))

    def _on_cancel_plan(self) -> None:
        if self._editable and not DIALOG_JOBS.busy(self._run_id):
            log_action(self._who, "отменить план", self._run_id)
            DIALOG_JOBS.cancel_plan(self._run_id, self._who.session)
        self._tick()

    async def _on_expert_file(self, event: UploadEventArguments) -> None:
        """Файл эксперта: замечания — в очередь, согласия и оценки без текста — сводкой в чат."""
        name = Path(event.file.name).name
        data = await event.file.read()
        self._upload.reset()
        if not self._editable or DIALOG_JOBS.busy(self._run_id):
            return
        if not name.lower().endswith(".xlsx"):
            ui.notify("Нужен заполненный экспертом Excel этого прогона (.xlsx)", type="warning")
            return
        relname = f"dialog/expert/{name}"
        trace.save_bytes(self._run_id, relname, data)
        try:
            remarks = feedback.read_remarks(trace.run_dir(self._run_id) / relname)
        except Exception as error:
            ui.notify(f"Файл не разобран: {error}", type="negative")
            return
        queued = [r for r in remarks if feedback.is_queued(r)]
        dialog.save_remarks(self._run_id, name, queued)
        n = len(queued)
        log_action(self._who, "файл эксперта", self._run_id, file=name, queued=n)
        text = (f"Загружен файл эксперта «{name}»: в очереди {n} {plural(n, 'замечание', 'замечания', 'замечаний')} "
                "(несогласия и замечания с текстом). Разбор по одному — кнопка «Следующее замечание эксперта».")
        summary = feedback.summary(remarks)
        dialog.append_chat(self._run_id, "сервис", text + (f"\n{summary}" if summary else ""), session=self._who.session)
        self._tick()


@ui.page(LOGIN_PATH)
def page_login(request: Request) -> Response | None:
    """
    Экран входа: логин, пароль и одна кнопка.

    Пользователь один на всех, пароль лежит в .env.secrets. При промахе — всегда
    одно и то же сообщение, без подсказок о том, что именно не сошлось.
    """
    if nicegui_app.storage.user.get(SESSION_AUTHENTICATED, False):
        # Уже вошёл — на форме входа ему делать нечего
        return RedirectResponse(safe_referrer(nicegui_app.storage.user.get(SESSION_REFERRER)))

    ui.page_title("Платформа ОРВ · П-10")
    # Адрес посетителя — ключ счётчика попыток; за прокси его может не быть вовсе
    address = request.client.host if request.client is not None else "адрес неизвестен"
    who = _who(request)

    def try_login() -> None:
        """Проверить пару и, если сошлось, пустить туда, куда человек шёл."""
        now = time.monotonic()
        waiting = LOGIN_THROTTLE.retry_after(address, now)
        if waiting > 0:
            message.set_text(throttle_message(waiting))
            log_action(who, "неудачный вход", login=login.value or "", reason="пауза после ошибок")
            return

        if not check_credentials(login.value or "", password.value or ""):
            LOGIN_THROTTLE.register_failure(address, now)
            log_action(who, "неудачный вход", login=login.value or "")
            password.set_value("")
            message.set_text(LOGIN_ERROR)
            return

        LOGIN_THROTTLE.register_success(address)
        log_action(who, "вход")
        nicegui_app.storage.user[SESSION_AUTHENTICATED] = True
        ui.navigate.to(safe_referrer(nicegui_app.storage.user.pop(SESSION_REFERRER, None)))

    with ui.header().classes("items-center px-6 py-3"):
        ui.label("Платформа ОРВ · модуль П-10").classes("text-lg font-medium")

    with ui.column().classes("w-full items-center p-8"):
        with ui.card().classes("w-full max-w-sm p-6 gap-4"):
            ui.label("Вход").classes("text-xl font-medium")
            ui.label("Страница закрыта паролем. Доступ выдаёт владелец сервиса.").classes(
                "text-sm text-gray-700"
            )
            login = ui.input("Логин").props("outlined autofocus").classes("w-full")
            password = ui.input("Пароль", password=True).props("outlined").classes("w-full")
            message = ui.label("").classes("text-sm text-red-700")
            ui.button("Войти", on_click=try_login).props("unelevated").classes("w-full")

    # Enter в любом из полей — то же самое, что нажать «Войти»
    login.on("keydown.enter", try_login)
    password.on("keydown.enter", try_login)
    return None


@ui.page("/")
def page_index(request: Request) -> None:
    """Экран 1 — загрузка акта и запуск прогона."""
    who = _who(request)
    _layout(who)
    pending: dict[str, object] = {}
    act_types: set[str] = set()
    type_boxes: dict[str, ui.checkbox] = {}

    def refresh_start() -> None:
        """Кнопка активна, только когда есть файл и тип акта (2.7)."""
        if can_start("data" in pending, act_types):
            start.enable()
        else:
            start.disable()

    def on_type(changed: str, checked: bool) -> None:
        """Отметка типа акта: «нет подходящего типа» снимает остальные и наоборот."""
        nonlocal act_types
        act_types = toggle_act_type(act_types, changed, checked)
        for name, box in type_boxes.items():
            if box.value != (name in act_types):
                box.set_value(name in act_types)
        refresh_start()

    async def handle_upload(event: UploadEventArguments) -> None:
        """Принять файл: проверить и запомнить содержимое до нажатия кнопки."""
        error = validate_upload(event.file.name, event.file.size())
        if error is not None:
            ui.notify(error, type="negative")
            upload.reset()
            chosen.set_text("")
            pending.clear()
            refresh_start()
            return
        pending["name"] = event.file.name
        pending["data"] = await event.file.read()
        chosen.set_text(f"Выбран файл: {event.file.name}")
        refresh_start()

    def handle_rejected(_: UiEventArguments) -> None:
        """Файл не прошёл проверку ещё в браузере (расширение или размер)."""
        ui.notify(
            f"Файл не принят: нужен .docx, .pdf или .doc размером до {MAX_UPLOAD_MB} МБ.",
            type="negative",
        )

    def start_run() -> None:
        """Создать папку прогона, положить акт в трейс и поставить прогон в очередь."""
        name = pending.get("name")
        data = pending.get("data")
        if not isinstance(name, str) or not isinstance(data, bytes):
            ui.notify("Сначала выберите файл акта", type="warning")
            return
        if not act_types:
            ui.notify("Отметьте тип акта", type="warning")
            return
        types = [t for t in ACT_TYPES if t in act_types]          # порядок — как на странице
        # 23.10 (владелец): «Настройки прогона» убраны со страницы — потолки поиска всегда по умолчанию из конфига
        limits = dict(SEARCH_LIMITS)
        run_id = trace.new_run(name, act_types=types, search_limits=limits)
        input_name = input_filename(name)
        trace.save_bytes(run_id, input_name, data)
        QUEUE.submit(run_id, trace.run_dir(run_id) / input_name)
        log_action(who, "старт прогона", run_id, file=name, size=len(data), act_types=types,
                   search_limits=limits)
        ui.navigate.to(f"/run/{run_id}")

    with ui.column().classes("w-full items-center p-8"):
        with ui.card().classes("w-full max-w-2xl p-6 gap-4"):
            ui.label("Карта гипотез по нормативному акту").classes("text-xl font-medium")
            ui.label(
                "Загрузите нормативный акт Москвы в формате .docx, .pdf или .doc. Сервис прогонит его "
                "через двенадцать индикаторов эффективного регулирования и соберёт Excel "
                "с картой гипотез и запросом показателей в орган-разработчик."
            ).classes("text-sm text-gray-700")
            ui.label("Прогон занимает около семи минут.").classes("text-sm text-gray-600")

            upload = (
                ui.upload(
                    on_upload=handle_upload,
                    on_rejected=handle_rejected,
                    auto_upload=True,
                    max_files=1,
                    max_file_size=MAX_UPLOAD_BYTES,
                    label="Перетащите файл акта или выберите его",
                )
                .props(f'accept="{",".join(ACCEPTED_EXTENSIONS)}" flat bordered')
                .classes("w-full")
            )
            chosen = ui.label("").classes("text-sm text-primary")

            # 2.7 (АМ): тип акта — от него зависят обязательные показатели бланка
            ui.label("Тип акта (можно несколько)").classes("text-sm font-medium")
            for name in ACT_TYPES:
                type_boxes[name] = ui.checkbox(
                    name, on_change=lambda e, n=name: on_type(n, bool(e.value)),
                ).classes("text-sm")

            start = ui.button("Проанализировать", on_click=start_run).props("unelevated")
            start.disable()


@ui.page("/run/{run_id}")
def page_run(run_id: str, request: Request) -> None:
    """Экран 2 — граф выполнения, он же экран 3 — результат по окончании прогона."""
    who = _who(request)
    _layout(who)

    if not trace.run_dir(run_id).is_dir():
        with ui.column().classes("w-full items-center p-8"):
            with ui.card().classes("w-full max-w-2xl p-6 gap-2"):
                ui.label("Такого прогона нет").classes("text-lg font-medium")
                ui.link("Посмотреть список прогонов", "/history")
        return

    log_action(who, "открытие прогона", run_id)
    _RunView(run_id, who)


@ui.page("/history")
def page_history(request: Request) -> None:
    """Экран 4 — последние прогоны, клик открывает результат старого прогона."""
    _layout(_who(request))
    items = list_history()

    with ui.column().classes("w-full items-center p-8"), ui.card().classes("w-full max-w-4xl p-0"):
        with ui.column().classes("w-full p-5 gap-1"):
            ui.label("История прогонов").classes("text-lg font-medium")
            ui.label("Клик по строке открывает результат прогона.").classes(
                "text-sm text-gray-600"
            )
        ui.separator()

        if not items:
            ui.label("Прогонов пока нет.").classes("p-5 text-sm text-gray-600")
            return

        with ui.row().classes(
            "w-full items-center flex-nowrap gap-4 px-5 py-2 text-xs text-gray-500"
        ):
            ui.label("Акт").classes("grow")
            ui.label("Дата").classes("w-36")
            ui.label("Длительность").classes("w-28")
            ui.label("Статус").classes("w-24")
            ui.label("Стоимость").classes("w-20 text-right")

        for item in items:
            row = ui.row().classes(
                "w-full items-center flex-nowrap gap-4 px-5 py-3 border-t "
                "cursor-pointer hover:bg-gray-50"
            )
            with row:
                ui.label(item.act_filename).classes("grow truncate")
                ui.label(format_moment(item.started_at)).classes("w-36 text-sm text-gray-700")
                ui.label(
                    format_elapsed(item.duration_s) if item.duration_s is not None else "—"
                ).classes("w-28 text-sm text-gray-700")
                ui.label(item.status).classes(
                    f"w-24 text-sm {_STATUS_CLASS.get(item.status, 'text-gray-700')}"
                )
                ui.label(format_cost(item.cost_usd)).classes("w-20 text-sm text-right")
            row.on("click", lambda _, target=item.run_id: ui.navigate.to(f"/run/{target}"))


def main() -> None:
    """
    Точка входа сервиса: страница на порту 8030 (юнит deploy/p10-orv.service).

    Доступы читаются и проверяются ДО подъёма страницы: без пароля сервис обязан
    не подняться, а не встать нараспашку в открытый интернет.
    """
    config.load_secrets()
    config.require_auth_settings()
    configure_storage_path()
    attach_service_log(config.LOGS_DIR / "service.log")
    app_logger.info("сервис запущен: порт %s", WEB_PORT)
    trace.log_service_event("service_started", port=WEB_PORT, pid=os.getpid(),
                            max_concurrent_runs=config.MAX_CONCURRENT_RUNS, model=config.MODEL)
    ui.run(
        host="0.0.0.0",
        port=WEB_PORT,
        title="Платформа ОРВ · П-10",
        favicon="📊",
        reload=False,
        show=False,
        storage_secret=config.storage_secret(),
    )


def configure_storage_path() -> None:
    """
    Направить файлы сессий NiceGUI в config.NICEGUI_STORAGE_DIR.

    NiceGUI читает путь один раз при импорте (переменная NICEGUI_STORAGE_PATH,
    иначе .nicegui/ в текущей папке) и хранит его атрибутом класса Storage —
    поэтому меняем атрибут до ui.run(), а не переменную окружения в команде
    запуска: путь задан кодом и не зависит от того, откуда запустили сервис.
    """
    Storage.path = config.NICEGUI_STORAGE_DIR
    Storage.path.mkdir(parents=True, exist_ok=True)


if __name__ in {"__main__", "__mp_main__"}:
    main()
