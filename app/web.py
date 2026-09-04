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

ИМЯ АКТА. Загруженный файл ложится в папку прогона под служебным именем
00_input.docx, и именно его видит оркестратор — в progress.json попадает
«00_input.docx». Оригинальное имя сохраняет trace.new_run() в meta.json, оттуда
его и берут шапка прогона, история и имя скачиваемого Excel.
"""

from __future__ import annotations

import asyncio
import hmac
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import app as nicegui_app
from nicegui import run as nicegui_run
from nicegui import ui
from nicegui.events import UiEventArguments, UploadEventArguments
from nicegui.storage import Storage
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app import config, trace
from app.config import ACCEPTED_EXTENSIONS, all_indicators
from app.contracts import NodeState, NodeStatus, RunMeta, RunProgress, RunStatus
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


def download_filename(act_filename: str) -> str:
    """
    Имя, под которым Excel уезжает аналитику в «Загрузки»: собрано из имени акта,
    чтобы после десяти прогонов там не лежало десять файлов result.xlsx.
    """
    stem = Path(act_filename).name.strip()
    for ext in ACCEPTED_EXTENSIONS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)].strip()
            break
    return f"{stem} — карта гипотез.xlsx" if stem else "карта гипотез.xlsx"


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
        """Экран результата: сводка прогона и кнопка скачивания Excel."""
        started = parse_moment(progress.started_at)
        finished = parse_moment(progress.finished_at)
        elapsed = (finished - started).total_seconds() if started and finished else 0.0
        excel = excel_file(self._run_id, progress)
        act = read_act_filename(self._run_id, progress)
        is_ok = progress.status == "готов"

        with self._result, ui.card().classes("w-full p-0"):
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
                    ui.button(
                        "Скачать Excel",
                        icon="download",
                        on_click=lambda: ui.download.file(excel, download_filename(act)),
                    ).props("unelevated")
                else:
                    ui.label("Файл Excel не собран — скачивать нечего.").classes(
                        "text-sm text-gray-600"
                    )


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
