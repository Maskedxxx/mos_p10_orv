"""
Версии прогона в диалоговом режиме (план итерации 3, шаг 2).

Корень папки прогона — текущая версия: весь нынешний код (страница, кнопка
скачивания, узлы) читает артефакты оттуда и ничего не знает о версиях.
В versions/ — снимки: v0 — исходный разбор сервиса, не меняется никогда;
vN — после N-го применённого изменения. Снимок — копия чистых артефактов
(~230 КБ): 03_verdicts/NN.json, 04_buckets.json, 05_hypotheses/NN.json,
06_map.json, 07_request.json, 08_result.xlsx.

Не входят в снимок: сырые ответы модели (не меняются, сырые ответы диалога
лежат в dialog/), run.log (держит открытым логгер), meta.json и progress.json
(ведёт пайплайн, контракты extra=forbid), вход, документ и паспорт (заморожены).

versions/journal.jsonl — журнал изменений, только дописывается;
versions/state.json — VersionState: текущая версия, счётчики номеров, пометки.
Почему снимки, а не проигрывание событий: состояние маленькое, изменений
десятки, а проигрывание требует детерминизма, которого нет — в цепочке
вызовы модели.
"""

from __future__ import annotations

from pathlib import Path

from app.contracts import Buckets, DataRequest, HypothesisMap, JournalEntry, VersionState
from app.trace import (
    BUCKETS_FILE, EXCEL_FILE, HYPOTHESES_DIR, MAP_FILE, REQUEST_FILE, VERDICTS_DIR, VERSIONS_DIR, load_json, log_event,
    run_dir, save_bytes, save_json,
)

_STATE = f"{VERSIONS_DIR}/state.json"
_JOURNAL = f"{VERSIONS_DIR}/journal.jsonl"
_SINGLE = (BUCKETS_FILE, MAP_FILE, REQUEST_FILE, EXCEL_FILE)
_NUMBERED = (VERDICTS_DIR, HYPOTHESES_DIR)   # внутри — NN.json, сырые NN.raw*.json не берутся


def artifact_files(base: Path) -> list[str]:
    """Относительные пути чистых артефактов версии, которые есть в папке base.
    14.10: и таблицы проверки индикаторов 03_verdicts/NN.checks.json — помощник читает их, и они должны
    совпадать с вердиктом своей версии (снимок, откат, «Принять» перепроверки)."""
    names = [name for name in _SINGLE if (base / name).is_file()]
    for folder in _NUMBERED:
        names += sorted(f"{folder}/{p.name}" for p in (base / folder).glob("[0-9][0-9].json"))
    names += sorted(f"{VERDICTS_DIR}/{p.name}" for p in (base / VERDICTS_DIR).glob("[0-9][0-9].checks.json"))
    return names


def _snapshot(run_id: str, n: int) -> None:
    root = run_dir(run_id)
    for rel in artifact_files(root):
        save_bytes(run_id, f"{VERSIONS_DIR}/v{n}/{rel}", (root / rel).read_bytes())


def load_state(run_id: str) -> VersionState | None:
    """Состояние версий; None — диалог по прогону ещё не открывался."""
    if not (run_dir(run_id) / _STATE).is_file():
        return None
    return load_json(run_id, _STATE, VersionState)


def ensure_v0(run_id: str) -> VersionState:
    """
    Создать версию 0 при первом обращении, дальше — вернуть состояние.

    Номера «проверка не завершена» закрепляются здесь те же, что стоят в исходном
    Excel, — иначе после удаления строки карты они сдвинутся (шаг 1.5).
    """
    state = load_state(run_id)
    if state is not None:
        return state
    from app.nodes.excel import unverified_row_ids   # тот же расчёт, что у исходной книги

    hyp_map = load_json(run_id, MAP_FILE, HypothesisMap)
    request = load_json(run_id, REQUEST_FILE, DataRequest)
    buckets = load_json(run_id, BUCKETS_FILE, Buckets)
    unverified = unverified_row_ids(hyp_map, buckets)
    taken = [row.row_id for row in hyp_map.rows] + list(unverified.values())
    state = VersionState(
        next_row_id=max(taken, default=0) + 1,
        next_item_num=max((int(it.num) for it in request.items), default=0) + 1,
        unverified_ids=unverified,
    )
    _snapshot(run_id, 0)
    save_json(run_id, _STATE, state)
    save_json(run_id, f"{VERSIONS_DIR}/v0/state.json", state)   # пометки версии — для отката
    log_event(run_id, "version_created", version=0)
    return state


def commit(run_id: str, state: VersionState, entries: list[JournalEntry]) -> VersionState:
    """
    Зафиксировать корень как следующую версию: снимок, строки журнала, состояние.
    Счётчики и пометки в state уже обновлены вызывающим — здесь только номер версии.
    """
    n = state.current + 1
    if any(e.version != n for e in entries):
        raise ValueError(f"строки журнала должны относиться к версии {n}")
    _snapshot(run_id, n)
    with open(run_dir(run_id) / _JOURNAL, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(e.model_dump_json() + "\n")
    new_state = state.model_copy(update={"current": n})
    save_json(run_id, _STATE, new_state)
    save_json(run_id, f"{VERSIONS_DIR}/v{n}/state.json", new_state)   # пометки версии — для отката
    log_event(run_id, "version_created", version=n, changes=[f"{e.kind}: {e.target}" for e in entries])
    return new_state


def version_state(run_id: str, n: int) -> VersionState | None:
    """Состояние на момент версии n (пометки «правлено» и «устарело» той версии)."""
    rel = f"{VERSIONS_DIR}/v{n}/state.json"
    return load_json(run_id, rel, VersionState) if (run_dir(run_id) / rel).is_file() else None


def read_journal(run_id: str) -> list[JournalEntry]:
    path = run_dir(run_id) / _JOURNAL
    if not path.is_file():
        return []
    return [JournalEntry.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def restore(run_id: str, n: int) -> None:
    """Вернуть корень к версии n побайтно: чистые артефакты корня заменяются снимком."""
    root, snap = run_dir(run_id), run_dir(run_id) / VERSIONS_DIR / f"v{n}"
    for rel in artifact_files(root):
        (root / rel).unlink()
    for rel in artifact_files(snap):
        save_bytes(run_id, rel, (snap / rel).read_bytes())
    log_event(run_id, "version_restored", version=n)


def recover(run_id: str) -> bool:
    """Если шаг прервался посередине и корень разошёлся с текущей версией — вернуть его. True — возвращали."""
    state = load_state(run_id)
    if state is None:
        return False
    root, snap = run_dir(run_id), run_dir(run_id) / VERSIONS_DIR / f"v{state.current}"
    same = artifact_files(root) == artifact_files(snap) and all(
        (root / rel).read_bytes() == (snap / rel).read_bytes() for rel in artifact_files(snap)
    )
    if not same:
        restore(run_id, state.current)
    return not same
