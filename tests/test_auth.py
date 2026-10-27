"""
Тесты входа по паролю (app/auth.py) и доступов сервиса (27.10 перенесены из tests/test_web.py вместе с кодом):
сверка пары, какие адреса открыты без входа, возврат на исходный адрес, задержка при подборе и загрузчик
.env.secrets. Пароля сервиса в тестах нет и быть не может — сверяется выдуманная пара, подставленная
в переменные окружения на время теста.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import auth, config


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
    assert auth.check_credentials(TEST_LOGIN, TEST_PASSWORD) is True


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
    assert auth.check_credentials(login, password) is False


def test_rejection_does_not_tell_which_half_was_wrong(fake_credentials):
    """Ошибка в логине и ошибка в пароле снаружи неразличимы."""
    wrong_login = auth.check_credentials("чужой", TEST_PASSWORD)
    wrong_password = auth.check_credentials(TEST_LOGIN, "не тот пароль")

    assert wrong_login is False
    assert wrong_password is False
    assert wrong_login == wrong_password
    # Сообщение на экране одно на оба случая: «или», а не «неверный пароль»
    assert auth.LOGIN_ERROR == "Неверный логин или пароль"
    assert " или " in auth.LOGIN_ERROR


def test_nobody_gets_in_when_credentials_are_not_configured(env_without_secrets):
    """Настройки пусты — не пускать никого, а не пускать всех."""
    assert auth.check_credentials("", "") is False
    assert auth.check_credentials("кто-нибудь", "что-нибудь") is False


# ---- 10. какие адреса открыты без входа ----


def test_login_page_is_open_without_session():
    assert auth.is_public_path(auth.LOGIN_PATH) is True


@pytest.mark.parametrize("path", ["/", "/history", "/run/20260825_080000_abcdef", "/garant", "/garant/abc123", "/feedback"])
def test_pages_are_closed_without_session(path: str):
    assert auth.is_public_path(path) is False


def test_brand_files_open_without_session():
    """27.10: шрифт и эмблемы нужны самой форме входа."""
    assert auth.is_public_path("/brand/fonts/TTMoscowEconomy-Regular.woff2") is True
    assert auth.is_public_path("/brand/login_band.svg") is True
    assert auth.is_public_path("/brandbook") is False


def test_ready_excel_is_closed_without_session():
    """Скачивание — такой же запрос: разбор акта по прямой ссылке не отдаётся."""
    assert auth.is_public_path("/_nicegui/auto/static/0123abcd/08_result.xlsx") is False
    assert auth.is_public_path("/_nicegui/auto/media/0123abcd/08_result.xlsx") is False


@pytest.mark.parametrize(
    "path",
    ["/_nicegui/3.16.0/static/vue.esm-browser.js", "/_nicegui_ws/socket.io/"],
)
def test_nicegui_own_files_stay_open(path: str):
    """Без скриптов и сокета NiceGUI не отрисовалась бы сама форма входа."""
    assert auth.is_public_path(path) is True


# ---- 11. возврат на адрес, куда человек шёл ----


def test_visitor_returns_to_the_page_he_was_going_to():
    assert auth.safe_referrer("/run/20260825_080000_abcdef") == "/run/20260825_080000_abcdef"
    assert auth.safe_referrer("/history") == "/history"


@pytest.mark.parametrize(
    "path",
    [None, "", "/login", "//чужой-сайт.ru", "http://чужой-сайт.ru", "чужой-сайт.ru", 42],
)
def test_referrer_falls_back_to_the_main_page(path: object):
    """Пусто, петля на вход и любой чужой адрес — все ведут на главную."""
    assert auth.safe_referrer(path) == "/"


def test_referrer_survives_the_whole_way_from_middleware_to_login():
    """Путь, который запомнил middleware, доходит до перехода после входа целым."""
    session: dict[str, object] = {}
    session[auth.SESSION_REFERRER] = "/run/20260825_080000_abcdef"

    target = auth.safe_referrer(session.pop(auth.SESSION_REFERRER, None))

    assert target == "/run/20260825_080000_abcdef"
    assert auth.SESSION_REFERRER not in session  # использован один раз и забыт


# ---- 12. задержка при подборе пароля ----

ADDRESS = "203.0.113.7"
OTHER_ADDRESS = "203.0.113.8"


def test_first_misses_are_forgiven_then_the_pause_starts():
    """Человек ошибся раскладкой — ждать не заставляем; подбор — заставляем."""
    throttle = auth.LoginThrottle()
    now = 1000.0

    for _ in range(config.LOGIN_FREE_ATTEMPTS):
        throttle.register_failure(ADDRESS, now)
        assert throttle.retry_after(ADDRESS, now) == 0.0

    throttle.register_failure(ADDRESS, now)

    assert throttle.retry_after(ADDRESS, now) == pytest.approx(config.LOGIN_DELAY_STEP_S)


def test_pause_grows_with_every_miss_and_stops_at_the_ceiling():
    throttle = auth.LoginThrottle()
    now = 1000.0

    for _ in range(config.LOGIN_FREE_ATTEMPTS + 2):
        throttle.register_failure(ADDRESS, now)
    assert throttle.retry_after(ADDRESS, now) == pytest.approx(2 * config.LOGIN_DELAY_STEP_S)

    for _ in range(100):
        throttle.register_failure(ADDRESS, now)
    assert throttle.retry_after(ADDRESS, now) == pytest.approx(config.LOGIN_DELAY_MAX_S)


def test_pause_runs_out_by_itself():
    """Пауза — это пауза, а не блокировка: выждал — пробуй снова."""
    throttle = auth.LoginThrottle()
    for _ in range(config.LOGIN_FREE_ATTEMPTS + 1):
        throttle.register_failure(ADDRESS, 1000.0)

    assert throttle.retry_after(ADDRESS, 1000.0) > 0
    assert throttle.retry_after(ADDRESS, 1000.0 + config.LOGIN_DELAY_STEP_S) == 0.0


def test_successful_login_resets_the_counter():
    throttle = auth.LoginThrottle()
    for _ in range(config.LOGIN_FREE_ATTEMPTS + 3):
        throttle.register_failure(ADDRESS, 1000.0)
    assert throttle.retry_after(ADDRESS, 1000.0) > 0

    throttle.register_success(ADDRESS)

    assert throttle.retry_after(ADDRESS, 1000.0) == 0.0
    # И следующий промах снова начинается с прощаемых попыток, а не с прежней паузы
    throttle.register_failure(ADDRESS, 1000.0)
    assert throttle.retry_after(ADDRESS, 1000.0) == 0.0


def test_pause_is_counted_per_address():
    throttle = auth.LoginThrottle()
    for _ in range(config.LOGIN_FREE_ATTEMPTS + 1):
        throttle.register_failure(ADDRESS, 1000.0)

    assert throttle.retry_after(ADDRESS, 1000.0) > 0
    assert throttle.retry_after(OTHER_ADDRESS, 1000.0) == 0.0


def test_quiet_address_is_forgotten_and_nobody_is_blocked_forever():
    throttle = auth.LoginThrottle()
    for _ in range(config.LOGIN_FREE_ATTEMPTS + 5):
        throttle.register_failure(ADDRESS, 1000.0)

    later = 1000.0 + config.LOGIN_ATTEMPT_TTL_S + 1

    assert throttle.retry_after(ADDRESS, later) == 0.0
    # Серия началась заново, а не продолжила прежний счёт
    throttle.register_failure(ADDRESS, later)
    assert throttle.retry_after(ADDRESS, later) == 0.0


def test_throttle_message_tells_how_long_to_wait():
    assert "12" in auth.throttle_message(11.2)
    assert "1" in auth.throttle_message(0.1)


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
