"""
Трейсы прогона сервиса П-10 (шаг 1).

Отвечает за папку runs/<run_id>/ и запись всех артефактов пайплайна:

    00_input.docx
    01_document.md
    02_summary.md
    03_verdicts/01.json .. 12.json
    03_verdicts/01.raw.json .. 12.raw.json     сырой ответ claude -p целиком
    04_buckets.json
    05_hypotheses/*.json
    06_map.json
    07_request.json
    08_result.xlsx
    run.log
    meta.json
    events.jsonl                               журнал сессии (2.7): одно событие — одна строка

Требование владельца: сырой ответ (`save_raw`) сохраняется ВСЕГДА, включая упавшие
попытки — иначе нечем отлаживать. Повторная попытка с тем же именем не затирает
предыдущую, а дописывает суффикс `.attemptN`.

ВСЕ артефакты пишутся атомарно (см. _write_atomic): временный файл в той же папке +
os.replace. Иначе читатель, работающий параллельно с прогоном (страница прогона
опрашивает progress.json все семь минут), ловит обрывки файла. Замер на гонке
"поток пишет / поток читает и парсит" до правки: 182 319 битых чтений из 200 739
(90,8%). Правка сделана во всём модуле разом, а не только для progress.json:
стоит она ничего, а читателей артефактов со временем станет больше.

Журнал сессии events.jsonl (2.7, мониторинг): одна строка — одно событие — время, кто
(система, узел, агент, помощник, пользователь), что произошло, подробности. В одном файле —
создание сессии, каждый записанный артефакт (save_*), узлы пайплайна со входами и выходами
(граф), вызовы агентов с попытками, реплики чата, версии, действия пользователя. Файл
только дописывается; сбой записи журнала работу не останавливает.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from app.config import RUNS_DIR
from app.contracts import RunMeta

ModelT = TypeVar("ModelT", bound=BaseModel)

EVENTS_FILE = "events.jsonl"
# Служебные файлы, которые пишутся часто (каждое изменение прогресса, статус диалога), и снимки
# версий (о них — событие version_created): как артефакты не журналируются, иначе шум
_NOT_ARTIFACTS = {"meta.json", "progress.json", "dialog/status.json", EVENTS_FILE}
_NOT_ARTIFACT_DIRS = ("versions/",)
_events_lock = threading.Lock()   # события приходят из потоков индикаторов, веток гипотез и страницы


def _generate_run_id() -> str:
    """Сгенерировать id прогона: дата-время + короткий случайный суффикс."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = secrets.token_hex(3)
    return f"{timestamp}_{suffix}"


def _write_atomic(path: Path, text: str) -> None:
    """
    Записать текст в файл атомарно: сначала во временный файл В ТОЙ ЖЕ папке,
    затем os.replace() — на POSIX это атомарная подмена имени. Читатель всегда
    видит либо старую версию файла целиком, либо новую целиком, промежуточного
    состояния не существует.

    Обычный path.write_text() так не умеет: он открывает целевой файл на
    усечение и наполняет его по мере записи — читатель, попавший в этот момент,
    получает обрывок (замер на гонке — см. docstring модуля).

    Временный файл создаётся ИМЕННО в папке назначения: os.replace атомарен
    только в пределах одной файловой системы, через /tmp подмена выродилась бы
    в обычное копирование. Уникальный суффикс в имени нужен, чтобы параллельные
    писатели (двенадцать индикаторов пишут свои артефакты одновременно) не
    мешали друг другу. При любой ошибке записи временный файл убирается, а
    целевой остаётся нетронутым в прежнем виде.
    """
    tmp_path = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """
    То же, что _write_atomic, но для двоичных данных: временный файл в той же
    папке + os.replace. Причины и замеры — в docstring _write_atomic.
    """
    tmp_path = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def run_dir(run_id: str) -> Path:
    """Путь к папке прогона."""
    return RUNS_DIR / run_id


def log_event(run_id: str, event: str, *, who: str = "система", **fields: Any) -> None:
    """
    Дописать событие в журнал сессии runs/<run_id>/events.jsonl.

    Журнал — наблюдение, а не работа: нет папки прогона или запись не удалась — событие
    пропускается, вызывающий код идёт дальше.
    """
    path = run_dir(run_id)
    if not path.is_dir():
        return
    entry = {"ts": datetime.now().isoformat(timespec="milliseconds"), "event": event, "who": who, **fields}
    line = json.dumps(entry, ensure_ascii=False, default=str)
    try:
        with _events_lock, open(path / EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def read_events(run_id: str) -> list[dict]:
    """Прочитать журнал сессии целиком (для проверок и инструментов мониторинга)."""
    path = run_dir(run_id) / EVENTS_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _log_artifact(run_id: str, path: Path) -> None:
    """Событие «записан артефакт»: путь относительно папки прогона и размер."""
    rel = path.relative_to(run_dir(run_id)).as_posix()
    if rel in _NOT_ARTIFACTS or rel.startswith(_NOT_ARTIFACT_DIRS):
        return
    log_event(run_id, "artifact", path=rel, bytes=path.stat().st_size)


def new_run(
    act_filename: str,
    *,
    act_types: list[str] | None = None,
    comparison_regions: list[str] | None = None,
    search_limits: dict[int, int] | None = None,
    developers: list[str] | None = None,
) -> str:
    """
    Создать папку прогона и стартовый meta.json. Возвращает run_id.

    2.7 (итерация 4): выбор пользователя при загрузке — тип акта (АМ) и «Настройки
    прогона» (регионы сравнения — К, потолки поиска — Л) — хранится в meta.json и
    действует во всём прогоне и в диалоге по нему.
    """
    run_id = _generate_run_id()
    path = run_dir(run_id)
    path.mkdir(parents=True, exist_ok=False)

    meta = RunMeta(
        run_id=run_id,
        act_filename=act_filename,
        started_at=datetime.now().isoformat(),
        finished_at=None,
        cost_usd=0.0,
        status="started",
        current_node=None,
        act_types=list(act_types or []),
        comparison_regions=comparison_regions,
        search_limits=search_limits,
        developers=list(developers or []),
    )
    save_json(run_id, "meta.json", meta)
    log_event(run_id, "session_created", act_filename=act_filename, act_types=meta.act_types,
              comparison_regions=comparison_regions, search_limits=search_limits, developers=meta.developers)
    return run_id


def save_text(run_id: str, relname: str, text: str) -> Path:
    """Сохранить текстовый артефакт прогона (например, 01_document.md)."""
    path = run_dir(run_id) / relname
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, text)
    _log_artifact(run_id, path)
    return path


def save_bytes(run_id: str, relname: str, data: bytes) -> Path:
    """
    Сохранить двоичный артефакт прогона — загруженный аналитиком акт 00_input.docx.
    Он такая же часть трейса, как и остальные артефакты, и пишется так же атомарно.
    """
    path = run_dir(run_id) / relname
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(path, data)
    _log_artifact(run_id, path)
    return path


def save_json(run_id: str, relname: str, obj: BaseModel | Any) -> Path:
    """
    Сохранить JSON-артефакт. Принимает pydantic-модель или обычный объект.
    UTF-8, кириллица без escape-последовательностей (ensure_ascii=False), отступ 2.
    """
    path = run_dir(run_id) / relname
    path.parent.mkdir(parents=True, exist_ok=True)

    data = obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj
    _write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))
    _log_artifact(run_id, path)
    return path


def save_raw(run_id: str, relname: str, raw: str) -> Path:
    """
    Сохранить сырой ответ claude -p целиком. Сохраняется ВСЕГДА, включая упавшие
    попытки. Если файл с таким именем уже существует — не затирается, а пишется
    под именем с суффиксом попытки (.attempt2, .attempt3, ...).
    """
    path = run_dir(run_id) / relname
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        stem = path.stem
        suffix = path.suffix
        attempt = 2
        while True:
            candidate = path.parent / f"{stem}.attempt{attempt}{suffix}"
            if not candidate.exists():
                path = candidate
                break
            attempt += 1

    _write_atomic(path, raw)
    _log_artifact(run_id, path)
    return path


def load_json(run_id: str, relname: str, model_cls: type[ModelT]) -> ModelT:
    """Прочитать JSON-артефакт и провалидировать его в указанную pydantic-модель."""
    path = run_dir(run_id) / relname
    data = json.loads(path.read_text(encoding="utf-8"))
    return model_cls.model_validate(data)


def update_meta(run_id: str, **fields: Any) -> None:
    """
    Обновить meta.json, не теряя ранее записанные поля.

    Обновление проходит ПОЛНУЮ валидацию модели (в отличие от model_copy(update=...),
    который валидацию не делает): неизвестное имя поля или неверный тип значения —
    ValidationError, а не молчаливая потеря или порча данных. Если валидация не прошла,
    файл на диске остаётся нетронутым — запись происходит только после её успеха.
    """
    meta = load_json(run_id, "meta.json", RunMeta)
    merged = {**meta.model_dump(), **fields}
    updated = RunMeta.model_validate(merged)
    save_json(run_id, "meta.json", updated)
