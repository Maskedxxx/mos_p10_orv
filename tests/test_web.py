"""
Тесты веб-страницы сервиса П-10 (app/web.py).

Проверяется логика, отделённая от отрисовки: проверка загружаемого файла,
очередь прогонов, устойчивое чтение progress.json, имя скачиваемого файла и
список истории. Отрисовку тесты не поднимают намеренно — вся она построена на
этих функциях, а страница проверяется вживую запросом к работающему сервису.

Прогон в тестах подменяется поддельным: настоящий run_pipeline идёт семь минут
и стоит около десяти долларов.

Вход по паролю проверяется здесь же: сверка пары, какие адреса открыты без входа,
возврат на исходный адрес, задержка при подборе и загрузчик .env.secrets. Пароля
сервиса в тестах нет и быть не может — сверяется выдуманная пара, подставленная
в переменные окружения на время теста.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterable
from pathlib import Path

import pytest

from datetime import datetime

from app import config, trace, web
from app.actions import Who
from app.contracts import NodeState, RunMeta, RunProgress, RunSummary

# ---- вспомогательное ----


def _write_progress(
    runs_dir: Path,
    run_id: str,
    *,
    status: str = "готов",
    started_at: str = "2026-08-25T08:00:00",
    finished_at: str | None = "2026-08-25T08:07:04",
    act_filename: str = "Акт.docx",
    cost: float = 1.0,
    nodes: list[NodeState] | None = None,
    summary: RunSummary | None = None,
) -> Path:
    """Создать папку прогона с meta.json и progress.json — как их пишет оркестратор."""
    directory = runs_dir / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "act_filename": act_filename,
                "started_at": started_at,
                "status": "finished",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    progress = RunProgress(
        run_id=run_id,
        # оркестратор видит служебное имя файла в папке прогона, не имя акта
        act_filename=web.input_filename("акт.docx"),
        status=status,
        nodes=nodes or [],
        started_at=started_at,
        finished_at=finished_at,
        total_cost_usd=cost,
        summary=summary,
    )
    (directory / "progress.json").write_text(
        json.dumps(progress.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    return directory


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


async def _wait_released(queue: web.RunQueue, run_id: str, timeout: float = 5.0) -> None:
    """Дождаться, пока прогон покинет очередь (слот освободился)."""
    deadline = time.monotonic() + timeout
    while queue.state(run_id).status != "неизвестен":
        assert time.monotonic() < deadline, f"прогон {run_id} не завершился"
        await asyncio.sleep(0.01)


async def _finish_all(queue: web.RunQueue, runner: _BlockingRunner) -> None:
    """Отпустить все прогоны и дождаться опустошения очереди — чтобы цикл закрылся чисто."""
    for event in runner.release.values():
        event.set()
    for run_id in runner.started:
        await _wait_released(queue, run_id)
    await asyncio.sleep(0.05)


# ---- 1. проверка загружаемого файла ----


def test_upload_accepts_all_supported_formats():
    """Пайплайн принимает .docx, .pdf и .doc — все три идут в docling."""
    for name in ("акт.docx", "акт.pdf", "акт.doc", "АКТ.PDF", "Акт.DocX"):
        assert web.validate_upload(name, 1024) is None, name


def test_upload_rejects_unsupported_format():
    """Форматы вне списка отвергаются, в отказе перечислены допустимые."""
    message = web.validate_upload("акт.txt", 1024)
    assert message is not None
    assert "акт.txt" in message
    assert ".docx" in message and ".pdf" in message and ".doc" in message


def test_upload_rejects_file_without_extension():
    message = web.validate_upload("акт", 1024)
    assert message is not None


def test_upload_rejects_empty_file():
    message = web.validate_upload("акт.docx", 0)
    assert message is not None
    assert "пустой" in message


def test_upload_rejects_too_large_file():
    message = web.validate_upload("акт.docx", web.MAX_UPLOAD_BYTES + 1)
    assert message is not None
    assert str(web.MAX_UPLOAD_MB) in message


def test_upload_accepts_docx_regardless_of_extension_case():
    assert web.validate_upload("Приказ ДСМИ.DOCX", 80_000) is None


# ---- 2-4. очередь прогонов ----


def test_third_run_waits_in_queue_and_sees_its_position(monkeypatch):
    monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 2)

    async def scenario() -> None:
        runner = _BlockingRunner(["r1", "r2", "r3", "r4"])
        queue = web.RunQueue(runner)
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
        queue = web.RunQueue(runner)
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
        queue = web.RunQueue(runner)
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
        queue = web.RunQueue(runner)
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
        queue = web.RunQueue(slow_runner)
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


# ---- 5. устойчивое чтение progress.json ----


def test_read_progress_returns_none_when_run_folder_is_absent(tmp_runs_dir):
    assert web.read_progress("такого-прогона-нет") is None


def test_read_progress_returns_none_when_file_is_not_yet_written(tmp_runs_dir):
    (tmp_runs_dir / "20260825_080000_aaaaaa").mkdir()
    assert web.read_progress("20260825_080000_aaaaaa") is None


def test_read_progress_returns_none_on_broken_file(tmp_runs_dir):
    directory = tmp_runs_dir / "20260825_080000_bbbbbb"
    directory.mkdir()
    (directory / "progress.json").write_text('{"run_id": "обрыв', encoding="utf-8")
    assert web.read_progress("20260825_080000_bbbbbb") is None


def test_read_progress_reads_written_state(tmp_runs_dir):
    _write_progress(tmp_runs_dir, "20260825_080000_cccccc", status="идёт", finished_at=None)
    progress = web.read_progress("20260825_080000_cccccc")
    assert progress is not None
    assert progress.status == "идёт"


# ---- 6. имя скачиваемого файла ----


def test_download_filename_is_built_from_act_name():
    name = web.download_filename("Приказ ДСМИ от 09.08.2021 N 02-02-313.docx")
    assert name == "Приказ ДСМИ от 09.08.2021 N 02-02-313 — карта гипотез.xlsx"
    assert name != "result.xlsx"
    assert name.endswith(".xlsx")


def test_download_filename_survives_empty_act_name():
    assert web.download_filename(".docx") == "карта гипотез.xlsx"


def test_original_act_name_survives_saving_upload_as_input_docx(tmp_runs_dir):
    """Акт лежит как 00_input.docx, но имя для показа и скачивания — исходное."""
    run_id = trace.new_run("Приказ ДСМИ.docx")
    trace.save_bytes(run_id, web.input_filename("акт.docx"), b"PK\x03\x04 docx")

    assert (tmp_runs_dir / run_id / web.input_filename("акт.docx")).read_bytes() == b"PK\x03\x04 docx"
    assert web.read_act_filename(run_id) == "Приказ ДСМИ.docx"
    assert web.download_filename(web.read_act_filename(run_id)) == "Приказ ДСМИ — карта гипотез.xlsx"


# ---- 7. история прогонов ----


def test_history_skips_folders_without_progress_file(tmp_runs_dir):
    _write_progress(tmp_runs_dir, "20260825_080000_dddddd")
    (tmp_runs_dir / "20260101_120000_old111").mkdir()  # прогон до появления страницы
    (tmp_runs_dir / "20260101_130000_old222").mkdir()

    items = web.list_history()

    assert [item.run_id for item in items] == ["20260825_080000_dddddd"]


def test_history_is_ordered_from_new_to_old(tmp_runs_dir):
    _write_progress(tmp_runs_dir, "run_old", started_at="2026-08-20T10:00:00", finished_at="2026-08-20T10:05:00")
    _write_progress(tmp_runs_dir, "run_new", started_at="2026-08-25T09:00:00", finished_at="2026-08-25T09:07:04")
    _write_progress(tmp_runs_dir, "run_mid", started_at="2026-08-22T11:00:00", finished_at="2026-08-22T11:03:00")

    items = web.list_history()

    assert [item.run_id for item in items] == ["run_new", "run_mid", "run_old"]


def test_history_row_carries_act_name_duration_status_and_cost(tmp_runs_dir):
    _write_progress(
        tmp_runs_dir,
        "run_one",
        act_filename="Приказ ДСМИ.docx",
        started_at="2026-08-25T08:00:00",
        finished_at="2026-08-25T08:07:04",
        cost=16.38,
    )

    item = web.list_history()[0]

    assert item.act_filename == "Приказ ДСМИ.docx"
    assert item.duration_s == 424.0
    assert item.status == "готов"
    assert item.cost_usd == 16.38
    assert web.format_elapsed(item.duration_s) == "7 мин 04 сек"
    assert web.format_cost(item.cost_usd) == "$16.38"


def test_history_shows_unfinished_run_without_duration(tmp_runs_dir):
    _write_progress(tmp_runs_dir, "run_live", status="идёт", finished_at=None)

    item = web.list_history()[0]

    assert item.status == "идёт"
    assert item.duration_s is None


# ---- результат прогона ----


def test_excel_file_is_found_in_run_folder_even_after_failure(tmp_runs_dir):
    directory = _write_progress(tmp_runs_dir, "run_fail", status="сбой")
    (directory / web.EXCEL_FILE).write_bytes(b"xlsx")

    progress = web.read_progress("run_fail")

    assert web.excel_file("run_fail", progress) == directory / web.EXCEL_FILE


def test_excel_file_is_none_when_nothing_was_assembled(tmp_runs_dir):
    _write_progress(tmp_runs_dir, "run_empty", status="сбой")

    progress = web.read_progress("run_empty")

    assert web.excel_file("run_empty", progress) is None


def test_failed_node_tells_which_step_broke(tmp_runs_dir):
    nodes = [
        NodeState(key="convert", title="Конвертация акта в markdown", status="готов", duration_s=0.5),
        NodeState(key="passport", title="Паспорт акта", status="сбой", error="RuntimeError: нет ответа"),
    ]
    _write_progress(tmp_runs_dir, "run_broken", status="сбой", nodes=nodes)

    progress = web.read_progress("run_broken")
    assert progress is not None
    node = web.failed_node(progress)

    assert node is not None
    assert node.title == "Паспорт акта"
    assert "RuntimeError" in (node.error or "")


# ---- форматирование, которое видит аналитик ----


def test_step_duration_is_shown_in_seconds():
    assert web.format_seconds(0.5) == "0,5 сек"
    assert web.format_seconds(139.4) == "139 сек"


def test_elapsed_time_is_shown_in_minutes_and_seconds():
    assert web.format_elapsed(42) == "42 сек"
    assert web.format_elapsed(252) == "4 мин 12 сек"


def test_russian_plural_forms():
    assert web.plural(1, "гипотеза", "гипотезы", "гипотез") == "гипотеза"
    assert web.plural(3, "гипотеза", "гипотезы", "гипотез") == "гипотезы"
    assert web.plural(15, "гипотеза", "гипотезы", "гипотез") == "гипотез"
    assert web.plural(31, "показатель", "показателя", "показателей") == "показатель"


def test_queue_text_for_run_that_queue_does_not_know():
    unknown = web.QueueState("неизвестен", limit=2)
    assert web.queue_text(unknown, "готов") == ""
    assert "не отслеживается" in web.queue_text(unknown, "идёт")


# ---- 9. вход по паролю: сверка пары ----

# Выдуманная пара только для тестов. Настоящий логин и пароль живут исключительно
# в .env.secrets и в код — включая тесты — не попадают ни при каких обстоятельствах.
TEST_LOGIN = "аналитик"
TEST_PASSWORD = "пара-для-теста-не-настоящая"


@pytest.fixture
def fake_credentials(monkeypatch):
    """Подставить в окружение выдуманную пару доступа на время теста."""
    monkeypatch.setenv(config.ENV_AUTH_USER, TEST_LOGIN)
    monkeypatch.setenv(config.ENV_AUTH_PASSWORD, TEST_PASSWORD)


@pytest.fixture
def env_without_secrets(monkeypatch):
    """
    Убрать доступы из окружения на время теста и вернуть его потом как было.

    Через setenv + delenv, а не просто delenv: так monkeypatch запоминает исходное
    состояние даже когда переменной не было, и выставленное внутри теста (например
    загрузчиком .env.secrets) не утечёт в следующие тесты.
    """
    for name in (config.ENV_AUTH_USER, config.ENV_AUTH_PASSWORD, config.ENV_STORAGE_SECRET):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def test_correct_pair_is_accepted(fake_credentials):
    assert web.check_credentials(TEST_LOGIN, TEST_PASSWORD) is True


@pytest.mark.parametrize(
    ("login", "password"),
    [
        ("чужой", TEST_PASSWORD),               # логин не тот
        (TEST_LOGIN, "не тот пароль"),          # пароль не тот
        ("чужой", "не тот пароль"),             # не сошлось ничего
        ("", ""),                               # форма отправлена пустой
        (TEST_LOGIN.upper(), TEST_PASSWORD),    # регистр логина имеет значение
        (TEST_LOGIN, TEST_PASSWORD + " "),      # лишний пробел в пароле
        (TEST_LOGIN, TEST_PASSWORD[:-1]),       # почти угадал — всё равно нет
    ],
)
def test_wrong_pair_is_rejected(fake_credentials, login: str, password: str):
    assert web.check_credentials(login, password) is False


def test_rejection_does_not_tell_which_half_was_wrong(fake_credentials):
    """Ошибка в логине и ошибка в пароле снаружи неразличимы."""
    wrong_login = web.check_credentials("чужой", TEST_PASSWORD)
    wrong_password = web.check_credentials(TEST_LOGIN, "не тот пароль")

    assert wrong_login is False
    assert wrong_password is False
    assert wrong_login == wrong_password
    # Сообщение на экране одно на оба случая: «или», а не «неверный пароль»
    assert web.LOGIN_ERROR == "Неверный логин или пароль"
    assert " или " in web.LOGIN_ERROR


def test_nobody_gets_in_when_credentials_are_not_configured(env_without_secrets):
    """Настройки пусты — не пускать никого, а не пускать всех."""
    assert web.check_credentials("", "") is False
    assert web.check_credentials("кто-нибудь", "что-нибудь") is False


# ---- 10. какие адреса открыты без входа ----


def test_login_page_is_open_without_session():
    assert web.is_public_path(web.LOGIN_PATH) is True


@pytest.mark.parametrize("path", ["/", "/history", "/run/20260825_080000_abcdef"])
def test_pages_are_closed_without_session(path: str):
    assert web.is_public_path(path) is False


def test_ready_excel_is_closed_without_session():
    """Скачивание — такой же запрос: разбор акта по прямой ссылке не отдаётся."""
    assert web.is_public_path("/_nicegui/auto/static/0123abcd/08_result.xlsx") is False
    assert web.is_public_path("/_nicegui/auto/media/0123abcd/08_result.xlsx") is False


@pytest.mark.parametrize(
    "path",
    ["/_nicegui/3.16.0/static/vue.esm-browser.js", "/_nicegui_ws/socket.io/"],
)
def test_nicegui_own_files_stay_open(path: str):
    """Без скриптов и сокета NiceGUI не отрисовалась бы сама форма входа."""
    assert web.is_public_path(path) is True


# ---- 11. возврат на адрес, куда человек шёл ----


def test_visitor_returns_to_the_page_he_was_going_to():
    assert web.safe_referrer("/run/20260825_080000_abcdef") == "/run/20260825_080000_abcdef"
    assert web.safe_referrer("/history") == "/history"


@pytest.mark.parametrize(
    "path",
    [None, "", "/login", "//чужой-сайт.ru", "http://чужой-сайт.ru", "чужой-сайт.ru", 42],
)
def test_referrer_falls_back_to_the_main_page(path: object):
    """Пусто, петля на вход и любой чужой адрес — все ведут на главную."""
    assert web.safe_referrer(path) == "/"


def test_referrer_survives_the_whole_way_from_middleware_to_login():
    """Путь, который запомнил middleware, доходит до перехода после входа целым."""
    session: dict[str, object] = {}
    session[web.SESSION_REFERRER] = "/run/20260825_080000_abcdef"

    target = web.safe_referrer(session.pop(web.SESSION_REFERRER, None))

    assert target == "/run/20260825_080000_abcdef"
    assert web.SESSION_REFERRER not in session  # использован один раз и забыт


# ---- 12. задержка при подборе пароля ----

ADDRESS = "203.0.113.7"
OTHER_ADDRESS = "203.0.113.8"


def test_first_misses_are_forgiven_then_the_pause_starts():
    """Человек ошибся раскладкой — ждать не заставляем; подбор — заставляем."""
    throttle = web.LoginThrottle()
    now = 1000.0

    for _ in range(web.LOGIN_FREE_ATTEMPTS):
        throttle.register_failure(ADDRESS, now)
        assert throttle.retry_after(ADDRESS, now) == 0.0

    throttle.register_failure(ADDRESS, now)

    assert throttle.retry_after(ADDRESS, now) == pytest.approx(web.LOGIN_DELAY_STEP_S)


def test_pause_grows_with_every_miss_and_stops_at_the_ceiling():
    throttle = web.LoginThrottle()
    now = 1000.0

    for _ in range(web.LOGIN_FREE_ATTEMPTS + 2):
        throttle.register_failure(ADDRESS, now)
    assert throttle.retry_after(ADDRESS, now) == pytest.approx(2 * web.LOGIN_DELAY_STEP_S)

    for _ in range(100):
        throttle.register_failure(ADDRESS, now)
    assert throttle.retry_after(ADDRESS, now) == pytest.approx(web.LOGIN_DELAY_MAX_S)


def test_pause_runs_out_by_itself():
    """Пауза — это пауза, а не блокировка: выждал — пробуй снова."""
    throttle = web.LoginThrottle()
    for _ in range(web.LOGIN_FREE_ATTEMPTS + 1):
        throttle.register_failure(ADDRESS, 1000.0)

    assert throttle.retry_after(ADDRESS, 1000.0) > 0
    assert throttle.retry_after(ADDRESS, 1000.0 + web.LOGIN_DELAY_STEP_S) == 0.0


def test_successful_login_resets_the_counter():
    throttle = web.LoginThrottle()
    for _ in range(web.LOGIN_FREE_ATTEMPTS + 3):
        throttle.register_failure(ADDRESS, 1000.0)
    assert throttle.retry_after(ADDRESS, 1000.0) > 0

    throttle.register_success(ADDRESS)

    assert throttle.retry_after(ADDRESS, 1000.0) == 0.0
    # И следующий промах снова начинается с прощаемых попыток, а не с прежней паузы
    throttle.register_failure(ADDRESS, 1000.0)
    assert throttle.retry_after(ADDRESS, 1000.0) == 0.0


def test_pause_is_counted_per_address():
    throttle = web.LoginThrottle()
    for _ in range(web.LOGIN_FREE_ATTEMPTS + 1):
        throttle.register_failure(ADDRESS, 1000.0)

    assert throttle.retry_after(ADDRESS, 1000.0) > 0
    assert throttle.retry_after(OTHER_ADDRESS, 1000.0) == 0.0


def test_quiet_address_is_forgotten_and_nobody_is_blocked_forever():
    throttle = web.LoginThrottle()
    for _ in range(web.LOGIN_FREE_ATTEMPTS + 5):
        throttle.register_failure(ADDRESS, 1000.0)

    later = 1000.0 + web.LOGIN_ATTEMPT_TTL_S + 1

    assert throttle.retry_after(ADDRESS, later) == 0.0
    # Серия началась заново, а не продолжила прежний счёт
    throttle.register_failure(ADDRESS, later)
    assert throttle.retry_after(ADDRESS, later) == 0.0


def test_throttle_message_tells_how_long_to_wait():
    assert "12" in web.throttle_message(11.2)
    assert "1" in web.throttle_message(0.1)


# ---- 13. секреты: загрузчик .env.secrets и отказ стартовать без пароля ----


def _write_secrets(tmp_path: Path, body: str) -> Path:
    """Написать временный .env.secrets — настоящий файл тесты не трогают."""
    path = tmp_path / ".env.secrets"
    path.write_text(body, encoding="utf-8")
    return path


def test_secrets_file_fills_the_environment(tmp_path, env_without_secrets):
    path = _write_secrets(
        tmp_path,
        f"{config.ENV_AUTH_USER}={TEST_LOGIN}\n{config.ENV_STORAGE_SECRET}=клю=ч\n",
    )

    config.load_secrets(path)

    assert config.auth_user() == TEST_LOGIN
    # Делится только первый знак «=»: он может встречаться и внутри значения
    assert config.storage_secret() == "клю=ч"


def test_nicegui_sessions_live_in_var_dir(tmp_path, monkeypatch):
    """Сессии браузера NiceGUI пишутся в var/nicegui/, а не в .nicegui/ в корне репо (2026-09-04)."""
    from nicegui.storage import Storage

    import app.web as web

    target = tmp_path / "var" / "nicegui"
    monkeypatch.setattr(config, "NICEGUI_STORAGE_DIR", target)
    web.configure_storage_path()
    assert Storage.path == target
    assert target.is_dir()


def test_secrets_loader_skips_comments_and_blank_lines(tmp_path, env_without_secrets):
    path = _write_secrets(
        tmp_path,
        "# доступ к веб-странице П-10\n"
        "\n"
        f"   {config.ENV_AUTH_USER}={TEST_LOGIN}   \n"
        "   \n"
        f"# {config.ENV_AUTH_PASSWORD}=строка-закомментирована\n"
        "строка без знака равенства\n",
    )

    config.load_secrets(path)

    assert config.auth_user() == TEST_LOGIN
    # Закомментированную строку загрузчик не подобрал
    assert config.auth_password() == ""


def test_secrets_loader_does_not_overwrite_the_environment(tmp_path, env_without_secrets, monkeypatch):
    """Заданное снаружи (юнитом systemd, окружением теста) сильнее файла."""
    monkeypatch.setenv(config.ENV_AUTH_USER, "из-окружения")
    path = _write_secrets(tmp_path, f"{config.ENV_AUTH_USER}=из-файла\n")

    config.load_secrets(path)

    assert config.auth_user() == "из-окружения"


def test_secrets_loader_survives_a_missing_file(tmp_path, env_without_secrets):
    """Файла нет — не падаем: доступы могли прийти из окружения."""
    config.load_secrets(tmp_path / "такого-файла-нет.secrets")

    assert config.auth_user() == ""


def test_service_refuses_to_start_without_password(env_without_secrets, monkeypatch):
    """Без пароля сервис обязан не подняться, а не встать нараспашку в интернет."""
    monkeypatch.setenv(config.ENV_AUTH_USER, TEST_LOGIN)
    monkeypatch.setenv(config.ENV_STORAGE_SECRET, "ключ-подписи-для-теста")

    with pytest.raises(RuntimeError) as error:
        config.require_auth_settings()

    text = str(error.value)
    assert "не запущен" in text
    assert config.ENV_AUTH_PASSWORD in text
    assert str(config.SECRETS_FILE) in text


def test_startup_error_names_every_missing_key(env_without_secrets):
    with pytest.raises(RuntimeError) as error:
        config.require_auth_settings()

    text = str(error.value)
    for name in (config.ENV_AUTH_USER, config.ENV_AUTH_PASSWORD, config.ENV_STORAGE_SECRET):
        assert name in text


def test_service_starts_when_all_three_keys_are_set(env_without_secrets, monkeypatch):
    monkeypatch.setenv(config.ENV_AUTH_USER, TEST_LOGIN)
    monkeypatch.setenv(config.ENV_AUTH_PASSWORD, TEST_PASSWORD)
    monkeypatch.setenv(config.ENV_STORAGE_SECRET, "ключ-подписи-для-теста")

    config.require_auth_settings()  # молча проходит — значит доступы на месте

def test_input_filename_preserves_extension():
    """Расширение исходного акта сохраняется: docling выбирает читалку по нему."""
    assert web.input_filename("акт.docx") == "00_input.docx"
    assert web.input_filename("акт.pdf") == "00_input.pdf"
    assert web.input_filename("акт.doc") == "00_input.doc"
    assert web.input_filename("АКТ.PDF") == "00_input.pdf"


def test_input_filename_falls_back_for_unknown_extension():
    """Неизвестное расширение до узла не дойдёт (валидация), но запас нужен."""
    assert web.input_filename("акт.txt") == "00_input.docx"
    assert web.input_filename("акт") == "00_input.docx"


def test_download_filename_strips_any_accepted_extension():
    """Имя для «Загрузок» собирается без расширения исходника, каким бы оно ни было."""
    for name in ("Постановление 410-ПП.pdf", "Постановление 410-ПП.docx", "Постановление 410-ПП.doc"):
        assert web.download_filename(name) == "Постановление 410-ПП — карта гипотез.xlsx"


def test_download_filename_with_version():
    """Версия диалога — в имени файла, чтобы в Загрузках не копились одинаковые имена (шаг 1.5)."""
    assert web.download_filename("Приказ МАДИ.pdf", version=3) == "Приказ МАДИ — карта гипотез, версия 3.xlsx"
    assert web.download_filename("Приказ МАДИ.pdf") == "Приказ МАДИ — карта гипотез.xlsx"



# ======================================================================
# Диалог с результатом прогона (итерация 3, шаг 6) — логика панели
# ======================================================================

from app.contracts import (  # noqa: E402
    AppendRequest, BuildHypotheses, DeleteMapRow, EditText, Marks, RecheckIndicator, SetVerdict,
)
from app.operations import Change, StepResult  # noqa: E402


def test_dialog_lock_one_editor_until_release_or_timeout():
    lock = web.DialogLock(ttl_s=10)
    assert lock.acquire("r", "a", now=0)
    assert not lock.acquire("r", "b", now=5)
    assert lock.acquire("r", "a", now=8)        # окно-хозяин продлевает
    assert not lock.acquire("r", "b", now=15)
    assert lock.acquire("r", "b", now=19)       # хозяин пропал дольше ttl — правка переходит
    lock.release("r", "a")                      # чужой замок не снимается
    assert not lock.acquire("r", "a", now=20)
    lock.release("r", "b")
    assert lock.acquire("r", "a", now=21)
    assert lock.acquire("другой", "b", now=21)  # замок — на прогон, не на сервис


def test_op_title_is_human_readable():
    t = web.op_title(SetVerdict(kind="смена вердикта", indicator=7, verdict="нарушен", rationale="15 форм", reason="э"))
    assert t == "Сменить вердикт индикатора 7 на «нарушен»: 15 форм"
    t = web.op_title(EditText(kind="правка текста", sheet="строка карты", key="3", field="гипотеза",
                              new_text="коротко", reason="вода"))
    assert t == "Поправить текст — строка карты 3, поле «гипотеза»: «коротко»"
    assert web.op_title(DeleteMapRow(kind="удалить строку карты", row_id=4, reason="x")) == "Удалить строку карты 4"
    assert "агентом запроса" in web.op_title(AppendRequest(kind="дополнить запрос", row_ids=[8, 9], reason="x"))


def test_estimate_cost_from_raw_traces_of_the_run(tmp_runs_dir):
    rid = "run_cost"
    trace.save_bytes(rid, "03_verdicts/03.raw.json", json.dumps([{"type": "x"}, {"total_cost_usd": 2.2}]).encode())
    trace.save_bytes(rid, "05_hypotheses/06.raw.json", json.dumps([{"total_cost_usd": 0.3}]).encode())
    trace.save_bytes(rid, "05_hypotheses/09.raw.json", json.dumps([{"total_cost_usd": 0.5}]).encode())
    recheck = RecheckIndicator(kind="перепроверить индикатор", indicator=3, clarification="c", reason="r")
    assert web.estimate_cost(rid, recheck) == pytest.approx(2.2)
    assert web.estimate_cost(rid, BuildHypotheses(kind="построить гипотезы", indicator=7, reason="r")) == pytest.approx(0.4)
    append = AppendRequest(kind="дополнить запрос", row_ids=[8], reason="r")
    assert web.estimate_cost(rid, append) is None                     # сырого ответа нет — цена после выполнения
    assert web.cost_note(append, None) == "агент · цена — после выполнения"
    assert web.cost_note(recheck, 2.2) == "агент · ≈ $2.20"
    free = DeleteMapRow(kind="удалить строку карты", row_id=1, reason="r")
    assert web.estimate_cost(rid, free) is None and web.cost_note(free, None) == "без агента · бесплатно"


def test_pending_plan_only_right_after_assistant():
    op = SetVerdict(kind="смена вердикта", indicator=7, verdict="нарушен", rationale="15 форм", reason="э")
    chat = [{"role": "пользователь", "text": "?"}, {"role": "ассистент", "text": "сменю", "plan": [op.model_dump(mode="json")]}]
    assert web.pending_plan(chat) == [op]
    assert web.pending_plan(chat + [{"role": "сервис", "text": "План отменён"}]) == []
    assert web.pending_plan([{"role": "ассистент", "text": "ответ", "plan": []}]) == []


def test_stale_notes_show_only_new_marks():
    before = Marks(stale_rows={1: "старое"})
    after = Marks(stale_rows={1: "старое", 2: "вердикт инд. 7 изменён"}, stale_verdicts={7: "гипотез нет"})
    assert web.stale_notes(before, after) == ["Вердикты, инд. 7: гипотез нет", "строка карты 2: вердикт инд. 7 изменён"]


def test_step_text_short_and_long():
    short = StepResult(version=3, change=Change("Вердикты, инд. 7", "вердикт изменён", "соблюдён", "нарушен"))
    assert web.step_text(short) == "Версия 3. Вердикты, инд. 7 — вердикт изменён: «соблюдён» → «нарушен»"
    long = StepResult(version=4, change=Change("Карта гипотез, стр. 1", "текст поправлен", "а" * 90, "б" * 90))
    assert web.step_text(long).splitlines()[1:] == ["Было: " + "а" * 90, "Стало: " + "б" * 90]


def test_dialog_unavailable_for_unfinished_and_old_runs(tmp_runs_dir):
    from test_versions import _write_run
    rid = "20260925_120000_dddddd"
    _write_run(rid)
    ready = RunProgress.model_validate({"run_id": rid, "act_filename": "a.docx", "status": "готов",
                                        "started_at": "2026-09-25T12:00:00", "nodes": []})
    assert web.dialog_unavailable(rid, ready) is None
    assert "после успешного" in web.dialog_unavailable(rid, ready.model_copy(update={"status": "сбой"}))
    (trace.run_dir(rid) / "06_map.json").write_text('{"rows": [{"row_id": 1}]}', encoding="utf-8")
    assert web.dialog_unavailable(rid, ready) == web.DIALOG_OLD_RUN


def test_version_rows_newest_first(tmp_runs_dir):
    from app.operations import run_step
    from test_versions import _write_run
    rid = "20260925_120000_eeeeee"
    _write_run(rid)
    trace.save_json(rid, "meta.json", RunMeta(run_id=rid, act_filename="a.docx", started_at="2026-09-25T12:00:00",
                                              finished_at="2026-09-25T12:10:00", status="finished"))
    (trace.run_dir(rid) / "01_document.md").write_text("акт", encoding="utf-8")
    assert web.version_rows(rid) == []
    run_step(rid, EditText(kind="правка текста", sheet="строка карты", key="1", field="гипотеза", new_text="к", reason="в"))
    rows = web.version_rows(rid)
    assert [n for n, _ in rows] == [1, 0]
    assert rows[0][1].endswith("Карта гипотез, стр. 1 — переформулировано поле «гипотеза»")
    assert rows[1][1] == "исходный разбор сервиса"



# ---- Состояния панели: что идёт, что ждёт человека, чем кончилось ----

def _now():
    from datetime import datetime
    return datetime(2026, 9, 25, 13, 0, 30)


def test_panel_state_idle_work_wait_error_view():
    from app import dialog
    ps = web.panel_state([], None, True, 2, _now())
    assert (ps.tone, ps.title) == ("готово", "Готово") and "версия 2" in ps.detail
    status = {"state": "думает", "title": "Помощник думает…", "detail": "обычно 10–40 сек",
              "since": "2026-09-25T13:00:18", "server": dialog.SERVER_ID}
    ps = web.panel_state([], status, True, 2, _now())
    assert (ps.tone, ps.title, ps.detail) == ("работа", "Помощник думает…", "12 сек · обычно 10–40 сек")
    waiting = {**status, "state": "решение", "title": "Ждёт вашего решения", "detail": "проверьте «было — стало»"}
    ps = web.panel_state([], waiting, True, 2, _now())
    assert (ps.tone, ps.detail) == ("решение", "проверьте «было — стало»")
    ps = web.panel_state([], {**status, "server": "прежний процесс"}, True, 2, _now())
    assert (ps.tone, ps.title) == ("сбой", "Действие прервано")
    op = DeleteMapRow(kind="удалить строку карты", row_id=4, reason="x").model_dump(mode="json")
    ps = web.panel_state([{"role": "ассистент", "text": "удалю", "plan": [op]}], None, True, 2, _now())
    assert (ps.tone, ps.title) == ("решение", "Ждёт вашего решения")
    partial = {"role": "сервис", "text": "План применён частично: 1 из 2 шагов\nещё", "mark": "частично"}
    ps = web.panel_state([partial], None, True, 3, _now())
    assert (ps.tone, ps.title) == ("сбой", "План применён частично: 1 из 2 шагов")
    assert web.panel_state([], None, False, 2, _now()).tone == "просмотр"
    assert web.panel_state([], status, False, 2, _now()).tone == "работа"   # чужое действие видно и в окне просмотра


def test_plan_summary_texts():
    assert web.plan_summary(2, 2, 0, None, None, 3) == (
        "Готово: план применён (2 из 2 шагов). Файл — версия 3, скачать можно кнопкой вверху.", "готово")
    text, mark = web.plan_summary(3, 2, 1, None, None, 5)
    assert mark == "готово" and "(2 из 3 шагов, отклонено 1)" in text
    assert web.plan_summary(1, 0, 1, None, None, 2) == ("План завершён без изменений — файл остался в версии 2.", "отменено")
    assert web.plan_summary(2, 1, 0, 2, "диск", 4) == (
        "План применён частично: 1 из 2 шагов, файл — версия 4. Шаг 2 не выполнен — диск.", "частично")
    assert web.plan_summary(2, 0, 0, 1, "строки 9 нет", 4) == (
        "План не применён: шаг 1 не выполнен — строки 9 нет. Файл не изменён (версия 4).", "сбой")


def _wait(predicate):
    async def waiter():
        for _ in range(300):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("не дождались")
    return waiter()


def test_dialog_jobs_plan_waits_for_decision_on_server(tmp_runs_dir, monkeypatch):
    from app import dialog
    from app.operations import Candidate
    from test_versions import _write_run
    rid = "20260925_130000_ffffff"
    _write_run(rid)
    edit = EditText(kind="правка текста", sheet="строка карты", key="1", field="гипотеза", new_text="к", reason="в")
    recheck = RecheckIndicator(kind="перепроверить индикатор", indicator=3, clarification="c", reason="r")
    cand = Candidate(recheck, None, "соблюдён — старое", "нарушен — новое", 1.5)
    monkeypatch.setattr(web.operations, "run_step", lambda run_id, op: (
        StepResult(version=None, candidate=cand, cost_usd=1.5) if op is recheck
        else StepResult(version=1, change=Change("Карта гипотез, стр. 1", "переформулировано", "а", "к"))))
    monkeypatch.setattr(web.operations, "accept", lambda run_id, c: StepResult(
        version=2, change=Change("Вердикты, инд. 3", "перепроверен", "соблюдён", "нарушен"), cost_usd=1.5))

    async def scenario():
        jobs = web.DialogJobs()
        assert jobs.start(rid, jobs.apply_plan(rid, [edit, recheck], session="s-aaaaaa"))
        await _wait(lambda: jobs.waiting(rid) is not None)
        title, shown = jobs.waiting(rid)
        assert shown is cand and title.startswith("Шаг 2 из 2")
        assert dialog.read_status(rid)["state"] == "решение"
        assert not jobs.start(rid, jobs.turn(rid, "ещё"))   # по прогону уже идёт действие
        jobs.decide(rid, True)
        await _wait(lambda: not jobs.busy(rid))

    asyncio.run(scenario())
    chat = dialog.read_chat(rid)
    assert [m.get("mark") for m in chat] == [None, "шаг", "шаг", "готово"]
    assert {m.get("session") for m in chat} == {"s-aaaaaa"}   # кто применял план — у каждой строки
    assert chat[-1]["text"].startswith("Готово: план применён (2 из 2 шагов)")
    assert web.dialog_spent(chat) == pytest.approx(1.5)
    assert dialog.read_status(rid) is None


def test_dialog_jobs_failure_stops_plan_with_reason(tmp_runs_dir, monkeypatch):
    from app import dialog
    from test_versions import _write_run
    rid = "20260925_130000_gggggg"
    _write_run(rid)
    edit = EditText(kind="правка текста", sheet="строка карты", key="9", field="гипотеза", new_text="к", reason="в")

    def boom(run_id, op):
        raise web.operations.OperationError("строки 9 нет")

    monkeypatch.setattr(web.operations, "run_step", boom)

    async def scenario():
        jobs = web.DialogJobs()
        jobs.start(rid, jobs.apply_plan(rid, [edit, edit]))
        await _wait(lambda: not jobs.busy(rid))

    asyncio.run(scenario())
    chat = dialog.read_chat(rid)
    assert [m.get("mark") for m in chat] == [None, "сбой"]
    assert chat[-1]["text"] == "План не применён: шаг 1 не выполнен — строки 9 нет. Файл не изменён (версия 0)."
    assert dialog.read_status(rid) is None


# ---- 2.7 (итерация 4): тип акта и «Настройки прогона» ----


def test_toggle_act_type_no_type_is_exclusive():
    from app.web import toggle_act_type
    s = toggle_act_type(set(), "господдержка", True)
    s = toggle_act_type(s, "контрольно-надзорная деятельность", True)
    assert s == {"господдержка", "контрольно-надзорная деятельность"}
    assert toggle_act_type(s, "нет подходящего типа", True) == {"нет подходящего типа"}
    assert toggle_act_type({"нет подходящего типа"}, "господдержка", True) == {"господдержка"}
    assert toggle_act_type({"господдержка"}, "господдержка", False) == set()


def test_start_needs_file_and_type():
    """14.10: поле регионов сравнения убрано — старт зависит только от файла и типа акта."""
    from app.web import can_start
    assert not can_start(True, set())                      # без типа акта прогон не стартует
    assert not can_start(False, {"господдержка"})
    assert can_start(True, {"нет подходящего типа"})


def test_clamp_limits():
    from app.config import SEARCH_LIMIT_MAX, SEARCH_LIMITS
    from app.web import clamp_limits
    limits = clamp_limits({3: 999, 4: 0, 10: "абв", 11: 12.0})
    assert limits[3] == SEARCH_LIMIT_MAX and limits[4] == 1 and limits[10] == SEARCH_LIMITS[10]
    assert limits[11] == 12 and set(limits) == set(SEARCH_LIMITS)


# ---- 2.7: конец сессии (мониторинг, шаг 3) ----


def _ready_run(rid: str, status: str = "готов") -> RunProgress:
    from test_versions import _write_run
    _write_run(rid)
    progress = RunProgress.model_validate({"run_id": rid, "act_filename": "a.docx", "status": status,
                                           "started_at": "2026-10-13T10:00:00", "nodes": []})
    trace.save_json(rid, "progress.json", progress)
    trace.save_json(rid, "meta.json", RunMeta(run_id=rid, act_filename="a.docx", started_at="2026-10-13T10:00:00",
                                              status="finished"))
    return progress


def test_dialog_unavailable_for_closed_session(tmp_runs_dir):
    """Закрытая сессия: панель диалога не показывается — вместо неё строка «Сессия закрыта …»."""
    from app import sessions
    rid = "20261013_100000_aaaaaa"
    ready = _ready_run(rid)
    assert web.dialog_unavailable(rid, ready) is None
    sessions.close_session(rid, by="таймер", now=datetime(2026, 10, 13, 12, 0))
    text = web.dialog_unavailable(rid, ready)
    assert text.startswith("Сессия закрыта 13.10.2026 12:00") and "простой" in text and "скачать" in text


def test_dialog_jobs_refuse_closed_session(tmp_runs_dir):
    """Сервер отклоняет действие диалога по закрытой сессии, даже если окно его прислало."""
    from app import sessions
    rid = "20261013_100000_bbbbbb"
    _ready_run(rid)
    sessions.close_session(rid, by="пользователь")

    async def scenario():
        jobs = web.DialogJobs()
        assert jobs.start(rid, jobs.turn(rid, "ещё")) is False

    asyncio.run(scenario())


def test_close_by_user(tmp_runs_dir, tmp_path, monkeypatch):
    """Кнопка «Завершить работу»: нельзя, пока идёт разбор или действие чата; иначе — действие в журнал и закрытие."""
    from types import SimpleNamespace
    from app import sessions
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    who = Who(session="s-aaaaaa")
    running = "20261013_100000_cccccc"
    _ready_run(running, status="идёт")
    assert "Разбор ещё идёт" in web.close_by_user(who, running, SimpleNamespace(busy=lambda r: False))
    rid = "20261013_100000_dddddd"
    _ready_run(rid)
    assert "действие в чате" in web.close_by_user(who, rid, SimpleNamespace(busy=lambda r: True))
    assert not sessions.is_closed(rid)
    assert web.close_by_user(who, rid, SimpleNamespace(busy=lambda r: False)) is None
    assert sessions.is_closed(rid)
    events = trace.read_events(rid)
    assert [e["event"] for e in events[-2:]] == ["user_action", "session_closed"]
    assert events[-2]["action"] == "завершить работу" and events[-1]["by"] == "пользователь"
    assert web.close_by_user(who, rid, SimpleNamespace(busy=lambda r: False)) == "Сессия уже закрыта."


def test_close_idle_sessions_skips_busy_dialog(tmp_runs_dir, monkeypatch):
    """Фоновое закрытие по простою: сессия, по которой идёт действие диалога, не закрывается."""
    from types import SimpleNamespace
    from app import sessions
    a, b = "20261013_100000_eeeeee", "20261013_100000_ffffff"
    _ready_run(a)
    _ready_run(b)
    monkeypatch.setattr(sessions, "idle_sessions", lambda now: [a, b])
    closed = web.close_idle_sessions(datetime(2026, 10, 13, 12, 0), SimpleNamespace(busy=lambda r: r == b))
    assert closed == [a] and sessions.is_closed(a) and not sessions.is_closed(b)
    assert json.loads((trace.run_dir(a) / sessions.SESSION_SUMMARY_FILE).read_text(encoding="utf-8"))["closed_by"] == "таймер"


def test_sources_html_links_and_escapes():
    """Шаг 7 помощника: источники под ответом — адреса ссылками, остальное экранировано."""
    out = web.sources_html(["Вердикты, индикатор 3", "https://internet.garant.ru/#/document/70636256 — редакции <акта>"])
    assert 'href="https://internet.garant.ru/#/document/70636256"' in out and "&lt;акта&gt;" in out
    assert "Вердикты, индикатор 3" in out and web.sources_html([]) == ""


def test_limits_text():
    assert web.limits_text({"Экспорт": 21, "Постановка ссылки": 929, "Информация о документе": 299}) == \
        "Гарант в этом месяце: выгрузки 21 из 30, постановка ссылки 929 из 1000"
    assert "не удалось" in web.limits_text(None)
