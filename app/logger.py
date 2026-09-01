"""
Логирование сервиса П-10 (шаг 1).

Два уровня логов:
- общий лог приложения (`app_logger`) — для сообщений вне контекста конкретного прогона;
- отдельный файловый лог на каждый прогон — runs/<run_id>/run.log, создаётся через
  `get_run_logger(run_id)`.

Узел пишется через имя дочернего логера (стандартный механизм `Logger.getChild`),
например `get_run_logger(run_id).getChild("02_verdicts")`.

Без внешних библиотек и структурированных форматов — только стандартный `logging`.

ВАЖНО (шаг 2, правка после параллельного прогона): на шаге 4 двенадцать индикаторов
вызываются параллельно с ОДНИМ run_id, и каждый вызов runner.call() дёргает
get_run_logger(run_id) заново. Без блокировки и проверки "хендлер уже стоит" гонка
потоков между "снять старые хендлеры" и "добавить новый" приводит к тому, что
хендлеры накапливаются и каждая строка лога пишется несколько раз. Поэтому
get_run_logger идемпотентна (не пересоздаёт хендлер, если он уже указывает на
нужный файл) и потокобезопасна (проверка и создание — под одним локом).
"""

from __future__ import annotations

import logging
import os
import threading

from app.config import RUNS_DIR

# Формат строки: время · уровень · узел (имя логера) · сообщение
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

# Общий лог приложения
app_logger = logging.getLogger("p10_orv")
if not app_logger.handlers:
    _app_handler = logging.StreamHandler()
    _app_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    app_logger.addHandler(_app_handler)
    app_logger.setLevel(logging.INFO)

# Лок вокруг проверки "хендлер уже стоит" + его создания — без него параллельные
# вызовы get_run_logger с одним run_id (12 индикаторов шага 4) плодят дубликаты хендлеров
_lock = threading.Lock()


def get_run_logger(run_id: str) -> logging.Logger:
    """
    Вернуть логер прогона с файловым хендлером в runs/<run_id>/run.log.

    Идемпотентно: если у логера уже есть файловый хендлер на нужный файл —
    возвращается как есть, ничего не пересоздаётся. Потокобезопасно: проверка
    и создание хендлера выполняются под одним локом, чтобы параллельные вызовы
    с одним run_id не породили дубликаты хендлеров (и, как следствие, дубликаты
    каждой записи в лог).
    """
    run_path = RUNS_DIR / run_id
    log_path = run_path / "run.log"
    target = os.path.abspath(str(log_path))

    with _lock:
        run_path.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger(f"run.{run_id}")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        # Хендлер на нужный файл уже стоит — переиспользуем логер как есть
        for existing in logger.handlers:
            if isinstance(existing, logging.FileHandler) and existing.baseFilename == target:
                return logger

        # Иначе — закрываем старые хендлеры (если были на другой файл) и создаём новый
        for old_handler in list(logger.handlers):
            old_handler.close()
            logger.removeHandler(old_handler)

        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)

        return logger


def close_run_logger(logger: logging.Logger) -> None:
    """Закрыть файловые хендлеры логера прогона, чтобы файл не держался открытым."""
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
