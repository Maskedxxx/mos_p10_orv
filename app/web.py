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
import time
from collections.abc import Callable
from dataclasses import dataclass
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

from app import config, dialog, feedback, operations, trace, versions
from app.config import ACCEPTED_EXTENSIONS, all_indicators
from app.contracts import (
    Buckets, DataRequest, HypothesisMap, Marks, NodeState, NodeStatus, Operation, RunMeta, RunProgress, RunStatus,
)
from app.logger import get_run_logger
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
            task = asyncio.create_task(self._execute(job))
            # Ссылку на задачу держим сами: иначе её может забрать сборщик мусора
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _execute(self, job: _Job) -> None:
        """Один прогон в отдельном потоке, затем освобождение слота для следующего."""
        try:
            await nicegui_run.io_bound(self._runner, job.docx_path, job.run_id)
        except Exception as error:
            # Свои сбои прогон пишет в progress.json и run.log сам; сюда попадает
            # только то, что он не поймал — это в лог прогона, страницу не роняем
            get_run_logger(job.run_id).getChild("web").exception("прогон упал: %s", error)
        finally:
            self._running.remove(job.run_id)
            self._start_waiting()


# Единственная очередь сервиса: одна на процесс, общая для всех вкладок
QUEUE = RunQueue()


# ======================================================================
# Логика: диалог с результатом прогона (итерация 3)
# ======================================================================

# Как часто панель диалога перечитывает чат и версии с диска — второе окно видит то же
DIALOG_POLL_S = 2.0

# Сколько окно держит замок правки без продления: окно, пропавшее без отключения
# (сон ноутбука, обрыв сети), через это время отдаёт правку другому окну
DIALOG_LOCK_TTL_S = 15.0

DIALOG_OLD_RUN = "Диалог работает с прогонами версии 2.4 и новее — этот прогон собран по старым правилам."
DIALOG_READ_ONLY = "Этот прогон правят в другом окне — здесь только просмотр. Правка откроется, когда то окно закроют."

_OPERATION = TypeAdapter(Operation)


def dialog_unavailable(run_id: str, progress: RunProgress | None) -> str | None:
    """Почему диалог по прогону недоступен; None — доступен (завершён без сбоя, артефакты v2.4+)."""
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


def _logout() -> None:
    """Выход: сессия чистится целиком, дальше — только через страницу входа."""
    nicegui_app.storage.user.clear()
    ui.navigate.to(LOGIN_PATH)


def _layout() -> None:
    """Общая шапка сервиса: название, переходы между экранами и выход."""
    ui.page_title("Платформа ОРВ · П-10")
    with ui.header().classes("items-center justify-between px-6 py-3"):
        ui.label("Платформа ОРВ · модуль П-10").classes("text-lg font-medium")
        with ui.row().classes("gap-6 items-center"):
            ui.link("Новый прогон", "/").classes("text-white no-underline")
            ui.link("История", "/history").classes("text-white no-underline")
            ui.button("Выйти", on_click=_logout).props("flat dense color=white")


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

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
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
                _DialogView(self._run_id, on_version=self._refresh_result)
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
                        on_click=lambda: ui.download.file(excel, download_filename(act, version)),
                    ).props("unelevated")
                else:
                    ui.label("Файл Excel не собран — скачивать нечего.").classes(
                        "text-sm text-gray-600"
                    )


class _DialogView:
    """
    Панель «Работа с результатом» (итерация 3): чат с помощником, план с
    подтверждением, «было — стало» для шагов с агентом, версии файла, очередь
    замечаний эксперта.

    Источник правды — диск, как у графа: чат — dialog/chat.jsonl, версии —
    versions/. Панель перечитывает их по таймеру, поэтому второе окно того же
    прогона видит всё то же, но правит только одно окно (DIALOG_LOCK). Долгие
    вызовы (ход помощника, шаг с агентом) идут в потоке через nicegui.run.io_bound —
    страница не встаёт.
    """

    def __init__(self, run_id: str, on_version: Callable[[], None]) -> None:
        self._run_id = run_id
        self._on_version = on_version
        self._client_id = ui.context.client.id
        self._editable: bool | None = None
        self._busy = False
        self._session_cost = 0.0
        self._shown_chat = -1      # сколько реплик отрисовано
        self._shown_version = -1   # какая версия отрисована в списке версий
        self._decision: asyncio.Future | None = None   # ждёт «Принять» / «Отклонить»

        with ui.card().classes("w-full p-0"):
            with ui.row().classes("w-full items-center flex-nowrap gap-3 px-5 py-4"):
                ui.icon("forum", color="primary").classes("text-2xl")
                ui.label("Работа с результатом").classes("text-lg font-medium")
                ui.space()
                self._status = ui.label("").classes("text-sm text-gray-600 text-right")
            ui.separator()
            with ui.column().classes("w-full p-5 gap-3"):
                ui.label(
                    "Спросите, почему такой вердикт или откуда гипотеза; попросите поправить текст; "
                    "передайте решение эксперта или загрузите его заполненный файл. Каждое изменение — "
                    "новая версия Excel, исходная остаётся."
                ).classes("text-sm text-gray-700")
                self._lock_note = ui.label("").classes("text-sm text-amber-800")
                self._scroll = ui.scroll_area().classes("w-full h-96 border rounded")
                with self._scroll:
                    self._chat = ui.column().classes("w-full gap-1 p-2")
                self._pending = ui.column().classes("w-full gap-2")
                with ui.row().classes("items-center flex-nowrap gap-2") as self._busy_row:
                    ui.spinner(size="sm", color="primary")
                    self._busy_label = ui.label("").classes("text-sm text-gray-700")
                self._busy_row.set_visibility(False)
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
        client = ui.context.client
        client.on_disconnect(lambda: DIALOG_LOCK.release(self._run_id, self._client_id))
        client.on_delete(lambda: self._resolve(False))   # окно закрыли, не решив — кандидат не записан
        ui.timer(DIALOG_POLL_S, self._tick)
        self._tick()

    # ---- опрос диска ----

    def _tick(self) -> None:
        """Продлить замок, перечитать чат и версии, обновить кнопки."""
        editable = DIALOG_LOCK.acquire(self._run_id, self._client_id, time.monotonic())
        if editable != self._editable:
            self._editable = editable
            _RunView._set_text(self._lock_note, "" if editable else DIALOG_READ_ONLY)
            self._shown_chat = self._shown_version = -1   # кнопки плана и версий — только у правящего окна
        chat = dialog.read_chat(self._run_id)
        if len(chat) != self._shown_chat:
            self._shown_chat = len(chat)
            self._render_chat(chat)
            self._render_pending(chat)
        state = versions.load_state(self._run_id)
        current = state.current if state is not None else 0
        if current != self._shown_version:
            self._shown_version = current
            self._render_versions()
        self._render_controls(current)

    def _render_controls(self, current: int) -> None:
        active = bool(self._editable) and not self._busy
        for element in (self._input, self._send, self._upload_button, self._next):
            element.set_enabled(active)
        progress = dialog.remarks_progress(self._run_id)
        left = progress is not None and progress[0] < progress[1]
        self._next.set_visibility(left)
        if left:
            _RunView._set_text(self._next, f"Следующее замечание эксперта ({progress[0] + 1} из {progress[1]})")
        status = f"версия {current} · в этом окне потрачено {format_cost(self._session_cost)}"
        _RunView._set_text(self._status, status)

    def _render_chat(self, chat: list[dict]) -> None:
        self._chat.clear()
        with self._chat:
            if not chat:
                ui.label("Здесь будет переписка с помощником.").classes("text-sm text-gray-500 p-2")
            for message in chat:
                if message["role"] == "сервис":
                    ui.label(message["text"]).classes(
                        "w-full text-xs text-gray-700 whitespace-pre-line px-3 py-2 bg-gray-100 rounded"
                    )
                    continue
                mine = message["role"] == "пользователь"
                stamp = message.get("at", "")[11:16]
                if message.get("cost_usd"):
                    stamp += f" · {format_cost(message['cost_usd'])}"
                ui.chat_message(
                    text=html.escape(message["text"]).replace("\n", "<br>"), text_html=True,
                    name="Вы" if mine else "Помощник", sent=mine, stamp=stamp,
                ).classes("w-full")
        self._scroll.scroll_to(percent=1.0)

    def _render_pending(self, chat: list[dict]) -> None:
        """Карточка плана последнего ответа помощника: шаги, цена, «Применить» / «Отменить»."""
        self._pending.clear()
        plan = [] if self._busy else pending_plan(chat)
        if not plan:
            return
        costs = [estimate_cost(self._run_id, op) for op in plan]
        with self._pending, ui.card().classes("w-full p-4 gap-2 border-l-4 border-blue-400"):
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
                    ui.button("Применить", icon="check", on_click=lambda: self._apply_plan(plan)).props("unelevated")
                    ui.button("Отменить", on_click=self._cancel_plan).props("flat")

    def _render_versions(self) -> None:
        self._versions.clear()
        act = read_act_filename(self._run_id)
        rows = version_rows(self._run_id)
        with self._versions:
            if not rows:
                ui.label("Изменений ещё не было — файл в исходной версии.").classes("text-sm text-gray-600")
                return
            current = rows[0][0]
            for n, what in rows:
                path = trace.run_dir(self._run_id) / versions.VERSIONS / f"v{n}" / EXCEL_FILE
                with ui.row().classes("w-full items-start flex-nowrap gap-3"):
                    ui.label(f"v{n}" + (" · текущая" if n == current else "")).classes(
                        "text-sm font-medium w-28 shrink-0"
                    )
                    ui.label(what).classes("text-sm text-gray-700 flex-grow")
                    ui.button(
                        icon="download", on_click=lambda p=path, n=n: ui.download.file(p, download_filename(act, n))
                    ).props("flat dense").tooltip("Скачать эту версию")
                    if n != current and self._editable:
                        ui.button("Вернуть", on_click=lambda n=n: self._rollback(n)).props("flat dense").tooltip(
                            "Сделать новую версию с содержимым этой — ничего не стирается"
                        )

    # ---- действия ----

    def _set_busy(self, text: str | None) -> None:
        self._busy = text is not None
        self._busy_row.set_visibility(self._busy)
        _RunView._set_text(self._busy_label, text or "")
        self._shown_chat = -1   # перерисовать чат и карточку плана на следующем опросе
        self._tick()

    def _resolve(self, accepted: bool) -> None:
        if self._decision is not None and not self._decision.done():
            self._decision.set_result(accepted)

    async def _turn(self, text: str) -> None:
        """Ход помощника: ответ и, может быть, план — всё пишется в чат."""
        if pending_plan(dialog.read_chat(self._run_id)):
            dialog.append_chat(self._run_id, "сервис", "План выше не применён.")
        self._set_busy("Помощник думает… обычно 10–30 секунд")
        try:
            result = await nicegui_run.io_bound(dialog.turn, self._run_id, text)
            self._session_cost += result.cost_usd
        except Exception as error:   # сбой вне вызова модели (диск, чтение артефактов) — в чат, страница живёт
            dialog.append_chat(self._run_id, "сервис", f"Сбой хода помощника: {error}")
        finally:
            self._set_busy(None)

    async def _on_send(self) -> None:
        text = (self._input.value or "").strip()
        if not text or self._busy or not self._editable:
            return
        self._input.set_value("")
        await self._turn(text)

    async def _on_next_remark(self) -> None:
        if self._busy or not self._editable:
            return
        remark = dialog.next_remark(self._run_id)
        if remark is not None:
            await self._turn(feedback.remark_message(remark))

    async def _on_expert_file(self, event: UploadEventArguments) -> None:
        """Файл эксперта: замечания — в очередь, согласия и оценки без текста — сводкой в чат."""
        name = Path(event.file.name).name
        data = await event.file.read()
        self._upload.reset()
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
        text = (f"Загружен файл эксперта «{name}»: в очереди {n} {plural(n, 'замечание', 'замечания', 'замечаний')} "
                "(несогласия и замечания с текстом). Разбор по одному — кнопка «Следующее замечание эксперта».")
        summary = feedback.summary(remarks)
        dialog.append_chat(self._run_id, "сервис", text + (f"\n{summary}" if summary else ""))
        self._tick()

    def _cancel_plan(self) -> None:
        dialog.append_chat(self._run_id, "сервис", "План отменён — файл не изменён.")
        self._tick()

    async def _decide(self, title: str, candidate: operations.Candidate) -> bool:
        """Показать «было — стало» результата агента и дождаться «Принять» / «Отклонить»."""
        self._decision = asyncio.get_running_loop().create_future()
        self._pending.clear()
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
            with ui.row().classes("gap-2"):
                ui.button("Принять", icon="check", on_click=lambda: self._resolve(True)).props("unelevated")
                ui.button("Отклонить", on_click=lambda: self._resolve(False)).props("flat")
        try:
            return await self._decision
        finally:
            self._decision = None
            self._pending.clear()

    async def _apply_plan(self, plan: list[Operation]) -> None:
        """Шаги плана по одному: результат каждого — в чат; сбой останавливает остальные."""
        if self._busy or not self._editable:
            return
        rid, total = self._run_id, len(plan)
        dialog.append_chat(rid, "сервис", f"Применяю план: {total} {plural(total, 'шаг', 'шага', 'шагов')}.")
        for number, op in enumerate(plan, 1):
            title = f"Шаг {number} из {total}: {op_title(op)}"
            slow = " — агент работает, это может занять несколько минут" if operations.is_model_op(op) else ""
            self._set_busy(shorten(title, 160) + slow)
            before = versions.load_state(rid)
            try:
                result = await nicegui_run.io_bound(operations.run_step, rid, op)
                if result.candidate is not None:
                    self._session_cost += result.cost_usd
                    self._set_busy(None)
                    self._busy = True   # ввод закрыт, пока пользователь решает
                    self._render_controls(self._shown_version)
                    accepted = await self._decide(title, result.candidate)
                    if not accepted:
                        dialog.append_chat(rid, "сервис", f"Шаг {number} отклонён — файл не изменён.",
                                           cost_usd=round(result.cost_usd, 4))
                        continue
                    self._set_busy("Записываю…")
                    result = await nicegui_run.io_bound(operations.accept, rid, result.candidate)
            except Exception as error:   # OperationError — понятная причина; остальное — сбой, корень уже откатан
                why = str(error) if isinstance(error, operations.OperationError) else f"сбой: {error}"
                dialog.append_chat(rid, "сервис", f"Шаг {number} не выполнен — {why}. Остальные шаги плана отменены.")
                break
            notes = stale_notes(before.marks if before is not None else Marks(), versions.load_state(rid).marks)
            text = step_text(result) + ("\nУстарело: " + "; ".join(notes) if notes else "")
            dialog.append_chat(rid, "сервис", text, version=result.version, cost_usd=round(result.cost_usd, 4))
            self._on_version()
        self._set_busy(None)

    async def _rollback(self, n: int) -> None:
        """Вернуть версию n — новой версией, ничего не стирая."""
        if self._busy or not self._editable:
            return
        self._set_busy(f"Возвращаю версию {n}…")
        try:
            result = await nicegui_run.io_bound(operations.rollback, self._run_id, n)
            dialog.append_chat(self._run_id, "сервис", step_text(result), version=result.version)
            self._on_version()
        except Exception as error:
            dialog.append_chat(self._run_id, "сервис", f"Вернуть версию {n} не получилось: {error}")
        finally:
            self._set_busy(None)


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

    def try_login() -> None:
        """Проверить пару и, если сошлось, пустить туда, куда человек шёл."""
        now = time.monotonic()
        waiting = LOGIN_THROTTLE.retry_after(address, now)
        if waiting > 0:
            message.set_text(throttle_message(waiting))
            return

        if not check_credentials(login.value or "", password.value or ""):
            LOGIN_THROTTLE.register_failure(address, now)
            password.set_value("")
            message.set_text(LOGIN_ERROR)
            return

        LOGIN_THROTTLE.register_success(address)
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
def page_index() -> None:
    """Экран 1 — загрузка акта и запуск прогона."""
    _layout()
    pending: dict[str, object] = {}

    async def handle_upload(event: UploadEventArguments) -> None:
        """Принять файл: проверить и запомнить содержимое до нажатия кнопки."""
        error = validate_upload(event.file.name, event.file.size())
        if error is not None:
            ui.notify(error, type="negative")
            upload.reset()
            chosen.set_text("")
            start.disable()
            return
        pending["name"] = event.file.name
        pending["data"] = await event.file.read()
        chosen.set_text(f"Выбран файл: {event.file.name}")
        start.enable()

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
        run_id = trace.new_run(name)
        input_name = input_filename(name)
        trace.save_bytes(run_id, input_name, data)
        QUEUE.submit(run_id, trace.run_dir(run_id) / input_name)
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
            start = ui.button("Проанализировать", on_click=start_run).props("unelevated")
            start.disable()


@ui.page("/run/{run_id}")
def page_run(run_id: str) -> None:
    """Экран 2 — граф выполнения, он же экран 3 — результат по окончании прогона."""
    _layout()

    if not trace.run_dir(run_id).is_dir():
        with ui.column().classes("w-full items-center p-8"):
            with ui.card().classes("w-full max-w-2xl p-6 gap-2"):
                ui.label("Такого прогона нет").classes("text-lg font-medium")
                ui.link("Посмотреть список прогонов", "/history")
        return

    _RunView(run_id)


@ui.page("/history")
def page_history() -> None:
    """Экран 4 — последние прогоны, клик открывает результат старого прогона."""
    _layout()
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
