"""
Тесты трейсов прогона (app/trace.py): создание папки прогона, сохранение и
чтение текстовых/JSON-артефактов, сырых ответов и обновление meta.json,
а также атомарность записи артефактов (временный файл + os.replace).
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from pydantic import ValidationError

from app import trace
from app.contracts import RunMeta


def test_new_run_creates_folder_and_meta(tmp_runs_dir):
    run_id = trace.new_run("акт.docx")

    run_path = tmp_runs_dir / run_id
    assert run_path.is_dir()
    assert (run_path / "meta.json").is_file()

    meta = trace.load_json(run_id, "meta.json", RunMeta)
    assert meta.run_id == run_id
    assert meta.act_filename == "акт.docx"


def test_save_json_and_load_json_round_trip(tmp_runs_dir, valid_run_meta):
    run_id = "roundtrip_run"
    meta = RunMeta(**valid_run_meta)

    trace.save_json(run_id, "meta.json", meta)
    loaded = trace.load_json(run_id, "meta.json", RunMeta)

    assert loaded == meta


def test_save_json_writes_cyrillic_without_escapes(tmp_runs_dir, valid_run_meta):
    run_id = "cyrillic_run"
    meta = RunMeta(**{**valid_run_meta, "act_filename": "приказ.docx"})

    path = trace.save_json(run_id, "meta.json", meta)
    raw_text = path.read_text(encoding="utf-8")

    assert "приказ.docx" in raw_text
    assert "\\u" not in raw_text


def test_save_raw_does_not_overwrite_previous_attempt(tmp_runs_dir):
    run_id = "raw_run"

    first_path = trace.save_raw(run_id, "03_verdicts/01.raw.json", "первая попытка")
    second_path = trace.save_raw(run_id, "03_verdicts/01.raw.json", "вторая попытка")

    assert first_path != second_path
    assert first_path.read_text(encoding="utf-8") == "первая попытка"
    assert second_path.read_text(encoding="utf-8") == "вторая попытка"


def test_update_meta_preserves_previous_fields(tmp_runs_dir):
    run_id = trace.new_run("акт.docx")

    trace.update_meta(run_id, status="running")
    meta_after_first = trace.load_json(run_id, "meta.json", RunMeta)
    assert meta_after_first.status == "running"
    assert meta_after_first.act_filename == "акт.docx"

    trace.update_meta(run_id, current_node="02_verdicts")
    meta_after_second = trace.load_json(run_id, "meta.json", RunMeta)
    assert meta_after_second.current_node == "02_verdicts"
    assert meta_after_second.status == "running"
    assert meta_after_second.act_filename == "акт.docx"


def test_update_meta_unknown_field_raises_and_leaves_file_untouched(tmp_runs_dir):
    """Опечатка в имени поля должна падать с ошибкой, а не теряться молча."""
    run_id = trace.new_run("акт.docx")
    meta_path = tmp_runs_dir / run_id / "meta.json"
    original_content = meta_path.read_text(encoding="utf-8")

    with pytest.raises(ValidationError):
        trace.update_meta(run_id, sttus="опечатка")

    assert meta_path.read_text(encoding="utf-8") == original_content


def test_update_meta_invalid_type_raises_and_leaves_file_untouched(tmp_runs_dir):
    """Неверный тип значения должен падать с ошибкой, а не портить файл."""
    run_id = trace.new_run("акт.docx")
    meta_path = tmp_runs_dir / run_id / "meta.json"
    original_content = meta_path.read_text(encoding="utf-8")

    with pytest.raises(ValidationError):
        trace.update_meta(run_id, cost_usd="это не число")

    assert meta_path.read_text(encoding="utf-8") == original_content


# ---- Атомарность записи артефактов ----

# Длительность гонки и размер артефакта: файл должен быть достаточно большим,
# чтобы неатомарная запись гарантированно попадала в чужое чтение (замер до
# правки на таком же файле: 182 319 битых чтений из 200 739 — 90,8%)
_RACE_SECONDS = 2.0
_RACE_PAYLOAD_LEN = 20_000


def test_save_json_is_atomic_under_concurrent_reads(tmp_runs_dir):
    """
    Гонка настоящими потоками, ровно как будет работать страница прогона: один
    поток пишет артефакт в цикле, второй читает его и парсит. Битых чтений не
    должно быть НИ ОДНОГО — запись идёт во временный файл в той же папке и
    подменяется через os.replace.
    """
    run_id = "race_run"
    payload = "я" * _RACE_PAYLOAD_LEN
    trace.save_json(run_id, "progress.json", {"n": 0, "payload": payload})
    path = tmp_runs_dir / run_id / "progress.json"

    stop = threading.Event()
    broken: list[str] = []
    reads = 0

    def _writer() -> None:
        n = 0
        while not stop.is_set():
            n += 1
            trace.save_json(run_id, "progress.json", {"n": n, "payload": payload})

    def _reader() -> None:
        nonlocal reads
        while not stop.is_set():
            reads += 1
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                broken.append(f"{type(exc).__name__}: {exc}")
                continue
            # Разобранный JSON тоже обязан быть целым, а не обрезанным по полю
            if len(data.get("payload", "")) != _RACE_PAYLOAD_LEN:
                broken.append(f"неполный payload: {len(data.get('payload', ''))}")

    threads = [threading.Thread(target=_writer), threading.Thread(target=_reader)]
    for thread in threads:
        thread.start()
    time.sleep(_RACE_SECONDS)
    stop.set()
    for thread in threads:
        thread.join()

    print(f"\nгонка: чтений {reads}, битых {len(broken)}")
    assert reads > 500, f"читатель успел слишком мало чтений, гонки не было: {reads}"
    assert broken == [], f"битых чтений {len(broken)} из {reads}, например: {broken[:3]}"


def test_no_temporary_files_left_after_writes(tmp_runs_dir, valid_run_meta):
    """Временных файлов после записи не остаётся — в папке прогона только артефакты."""
    run_id = "tmp_files_run"

    trace.save_text(run_id, "01_document.md", "текст акта")
    trace.save_json(run_id, "meta.json", RunMeta(**valid_run_meta))
    trace.save_raw(run_id, "03_verdicts/01.raw.json", "первая попытка")
    trace.save_raw(run_id, "03_verdicts/01.raw.json", "вторая попытка")

    names = {path.name for path in (tmp_runs_dir / run_id).rglob("*") if path.is_file()}
    assert names == {
        "01_document.md", "meta.json", "01.raw.json", "01.raw.attempt2.json",
        "events.jsonl",   # журнал сессии (2.7) — тоже часть трейса, не временный файл
    }
    assert not [name for name in names if name.endswith(".tmp")]


def test_write_failure_leaves_previous_file_untouched(tmp_runs_dir, valid_run_meta, monkeypatch):
    """
    Исключение во время записи не портит целевой файл: он остаётся в прежнем
    виде, временный файл убирается, ошибка выходит наружу, а не проглатывается.
    """
    run_id = "write_fail_run"
    path = trace.save_json(run_id, "meta.json", RunMeta(**valid_run_meta))
    original = path.read_text(encoding="utf-8")

    def _boom(src, dst):
        raise OSError("подмена файла не удалась")

    monkeypatch.setattr(trace.os, "replace", _boom)

    with pytest.raises(OSError):
        trace.save_json(run_id, "meta.json", RunMeta(**{**valid_run_meta, "status": "новый"}))

    assert path.read_text(encoding="utf-8") == original
    assert [p.name for p in (tmp_runs_dir / run_id).iterdir()] == ["meta.json"]



def test_new_run_keeps_user_settings(tmp_runs_dir):
    """2.7: тип акта и настройки прогона — в meta.json; старый meta.json без них читается."""
    from app.contracts import RunMeta
    from app.trace import load_json, new_run
    rid = new_run("акт.docx", act_types=["господдержка"], comparison_regions=["Казань"], search_limits={3: 30})
    meta = load_json(rid, "meta.json", RunMeta)
    assert meta.act_types == ["господдержка"] and meta.comparison_regions == ["Казань"] and meta.search_limits == {3: 30}
    old = RunMeta(run_id="r", act_filename="a", started_at="t", status="finished")
    assert old.act_types == [] and old.comparison_regions is None and old.search_limits is None
