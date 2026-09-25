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
from app.trace import load_json, run_dir, save_bytes, save_json

VERSIONS = "versions"
_STATE = f"{VERSIONS}/state.json"
_JOURNAL = f"{VERSIONS}/journal.jsonl"
_SINGLE = ("04_buckets.json", "06_map.json", "07_request.json", "08_result.xlsx")
_NUMBERED = ("03_verdicts", "05_hypotheses")   # внутри — NN.json, сырые NN.raw*.json не берутся


def artifact_files(base: Path) -> list[str]:
    """Относительные пути чистых артефактов версии, которые есть в папке base."""
    names = [name for name in _SINGLE if (base / name).is_file()]
    for folder in _NUMBERED:
        names += sorted(f"{folder}/{p.name}" for p in (base / folder).glob("[0-9][0-9].json"))
    return names


def _snapshot(run_id: str, n: int) -> None:
    root = run_dir(run_id)
    for rel in artifact_files(root):
        save_bytes(run_id, f"{VERSIONS}/v{n}/{rel}", (root / rel).read_bytes())


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
    from app.nodes.excel import _unverified_row_ids   # тот же расчёт, что у исходной книги

    hyp_map = load_json(run_id, "06_map.json", HypothesisMap)
    request = load_json(run_id, "07_request.json", DataRequest)
    buckets = load_json(run_id, "04_buckets.json", Buckets)
    unverified = _unverified_row_ids(hyp_map, buckets)
    taken = [row.row_id for row in hyp_map.rows] + list(unverified.values())
    state = VersionState(
        next_row_id=max(taken, default=0) + 1,
        next_item_num=max((int(it.num) for it in request.items), default=0) + 1,
        unverified_ids=unverified,
    )
    _snapshot(run_id, 0)
    save_json(run_id, _STATE, state)
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
    return new_state


def read_journal(run_id: str) -> list[JournalEntry]:
    path = run_dir(run_id) / _JOURNAL
    if not path.is_file():
        return []
    return [JournalEntry.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def restore(run_id: str, n: int) -> None:
    """Вернуть корень к версии n побайтно: чистые артефакты корня заменяются снимком."""
    root, snap = run_dir(run_id), run_dir(run_id) / VERSIONS / f"v{n}"
    for rel in artifact_files(root):
        (root / rel).unlink()
    for rel in artifact_files(snap):
        save_bytes(run_id, rel, (snap / rel).read_bytes())


def recover(run_id: str) -> bool:
    """Если шаг прервался посередине и корень разошёлся с текущей версией — вернуть его. True — возвращали."""
    state = load_state(run_id)
    if state is None:
        return False
    root, snap = run_dir(run_id), run_dir(run_id) / VERSIONS / f"v{state.current}"
    same = artifact_files(root) == artifact_files(snap) and all(
        (root / rel).read_bytes() == (snap / rel).read_bytes() for rel in artifact_files(snap)
    )
    if not same:
        restore(run_id, state.current)
    return not same
