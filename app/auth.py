"""
Вход по паролю в сервис П-10 (27.10, модульность: вынесен из app/web.py без изменений).

Страницу выставляют в открытый интернет через туннель, поэтому она закрыта: в истории видны все разобранные акты.
Проверка сделана ОДНИМ местом — AuthMiddleware смотрит каждый входящий запрос и уводит незалогиненного на /login,
запомнив, куда тот шёл. Скачивание закрыто тем же местом: готовый Excel NiceGUI отдаёт по адресу /_nicegui/auto/...,
и этот префикс middleware не пропускает. Логин, пароль и ключ подписи сессий читает app/config.py из файла
.env.secrets — в коде их нет. Так же устроен официальный пример входа NiceGUI (middleware + app.storage.user);
готовые библиотеки входа (fastapi-users, Authlib) нужны для базы пользователей, а здесь один общий логин.

Пауза после неудачных входов — LoginThrottle: время передаётся снаружи, чтобы тесты не выжидали минуту.
Ограничители запросов (slowapi, limits) сюда не встают: вход идёт событием NiceGUI по веб-сокету, а не запросом.

Middleware регистрируется при импорте модуля: app/web.py импортирует его до ui.run() — после старта FastAPI
middleware добавить нельзя. Страница /login — в app/web.py.
"""

from __future__ import annotations

import hmac
import math

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import app as nicegui_app
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app import brand, config
from app.config import LOGIN_ATTEMPT_TTL_S, LOGIN_DELAY_MAX_S, LOGIN_DELAY_STEP_S, LOGIN_FREE_ATTEMPTS

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

# Пауза после неудачных входов: LOGIN_FREE_ATTEMPTS, LOGIN_DELAY_STEP_S, LOGIN_DELAY_MAX_S, LOGIN_ATTEMPT_TTL_S — в config.py


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

    Открыты ровно три вида адресов: страница входа, служебные файлы NiceGUI и файлы
    бренда (шрифт и эмблемы, 27.10) — без них форма входа не отрисовалась бы. Готовые
    файлы прогона лежат тоже под /_nicegui/, но закрыты: Excel с разбором акта не должен
    уезжать по прямой ссылке.
    """
    if path == LOGIN_PATH or path.startswith(brand.URL + "/"):
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
