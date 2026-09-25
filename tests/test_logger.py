"""
Тесты логирования прогона (app/logger.py): файловый хендлер на run.log
в папке прогона и освобождение файла после закрытия логера.
"""

from __future__ import annotations

from app.logger import close_run_logger, get_run_logger


def test_get_run_logger_writes_to_run_log_file(tmp_runs_dir):
    run_id = "log_write_run"
    logger = get_run_logger(run_id)

    logger.info("тестовое сообщение узла")
    close_run_logger(logger)

    log_path = tmp_runs_dir / run_id / "run.log"
    assert log_path.is_file()

    content = log_path.read_text(encoding="utf-8")
    assert "тестовое сообщение узла" in content
    assert "INFO" in content


def test_close_run_logger_releases_file(tmp_runs_dir):
    run_id = "log_close_run"
    logger = get_run_logger(run_id)
    handler = logger.handlers[0]

    close_run_logger(logger)

    assert logger.handlers == []
    assert handler.stream is None


def test_get_run_logger_thread_safe_under_concurrency(tmp_runs_dir):
    """
    Двенадцать потоков вызывают get_run_logger с ОДНИМ run_id и пишут по 30 строк —
    как это будет на шаге 4 (12 параллельных индикаторов). Без идемпотентности и
    лока хендлеры накапливаются гонкой, и каждая строка пишется несколько раз.
    Ожидание: ровно 360 строк в файле, ни одного исключения в потоках.
    """
    import threading

    run_id = "log_concurrent_run"
    n_threads = 12
    lines_per_thread = 30
    errors: list[BaseException] = []

    def worker(thread_num: int) -> None:
        try:
            logger = get_run_logger(run_id)
            for i in range(lines_per_thread):
                logger.info("поток %d строка %d", thread_num, i)
        except BaseException as exc:  # noqa: BLE001 - нужно поймать любое исключение потока
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == []

    log_path = tmp_runs_dir / run_id / "run.log"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads * lines_per_thread



def test_service_log_to_file_with_rotation(tmp_path):
    """Лог сервиса — в файл: сообщения приложения и предупреждения NiceGUI; служебный шум NiceGUI — нет."""
    import logging
    from logging.handlers import RotatingFileHandler

    from app.logger import app_logger, attach_service_log

    path = tmp_path / "logs" / "service.log"
    handler = attach_service_log(path)
    try:
        assert isinstance(handler, RotatingFileHandler)
        app_logger.info("сервис запущен: порт 8031")
        logging.getLogger("nicegui").info("служебный шум")
        logging.getLogger("nicegui").warning("ошибка обработчика")
        handler.flush()
        text = path.read_text(encoding="utf-8")
        assert "сервис запущен: порт 8031" in text and "ошибка обработчика" in text
        assert "служебный шум" not in text
    finally:
        app_logger.removeHandler(handler)
        logging.getLogger("nicegui").removeHandler(handler)
        handler.close()
