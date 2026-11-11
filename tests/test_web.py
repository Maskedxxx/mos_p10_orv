"""
Тесты веб-страницы сервиса П-10 (app/web.py).

Проверяется логика, отделённая от отрисовки: проверка загружаемого файла,
устойчивое чтение progress.json, имя скачиваемого файла, список истории, текст
шапки очереди и состояние панели диалога. Отрисовку тесты не поднимают намеренно —
вся она построена на этих функциях, а страница проверяется вживую запросом к
работающему сервису.

27.10 (модульность): тесты кода, вынесенного из app/web.py, — рядом с ним:
очередь прогонов — tests/test_run_queue.py, вход и доступы — tests/test_auth.py,
действия чата и конец сессии — tests/test_dialog_jobs.py, форматирование —
tests/test_formatting.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datetime import datetime

from app import trace, web
from app.contracts import DialogStatus, NodeState, RunMeta, RunProgress, RunSummary
from test_dialog_jobs import _entry, _ready_run

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


def test_queue_text_for_run_that_queue_does_not_know():
    unknown = web.QueueState("неизвестен", limit=2)
    assert web.queue_text(unknown, "готов") == ""
    assert "не отслеживается" in web.queue_text(unknown, "идёт")


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
    AppendRequest, DeleteMapRow, EditText, RecheckIndicator,
)


def test_agent_note_without_cost():
    """27.10: у шага плана — только делает ли его агент; стоимость эксперту не показываем."""
    recheck = RecheckIndicator(kind="перепроверить индикатор", indicator=3, clarification="c", reason="r")
    assert web.agent_note(recheck) == "агент"
    assert web.agent_note(AppendRequest(kind="дополнить запрос", row_ids=[8], reason="r")) == "агент"
    assert web.agent_note(DeleteMapRow(kind="удалить строку карты", row_id=1, reason="r")) == "без агента"


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
    status = DialogStatus(state="думает", title="Помощник думает…", detail="обычно 10–40 сек",
                          since="2026-09-25T13:00:18", server=dialog.SERVER_ID)
    ps = web.panel_state([], status, True, 2, _now())
    assert (ps.tone, ps.title, ps.detail) == ("работа", "Помощник думает…", "12 сек · обычно 10–40 сек")
    waiting = status.model_copy(update={"state": "решение", "title": "Ждёт вашего решения",
                                        "detail": "проверьте «было — стало»"})
    ps = web.panel_state([], waiting, True, 2, _now())
    assert (ps.tone, ps.detail) == ("решение", "проверьте «было — стало»")
    ps = web.panel_state([], status.model_copy(update={"server": "прежний процесс"}), True, 2, _now())
    assert (ps.tone, ps.title) == ("сбой", "Действие прервано")
    op = DeleteMapRow(kind="удалить строку карты", row_id=4, reason="x").model_dump(mode="json")
    ps = web.panel_state([_entry(role="ассистент", text="удалю", plan=[op])], None, True, 2, _now())
    assert (ps.tone, ps.title) == ("решение", "Ждёт вашего решения")
    partial = _entry(role="сервис", text="План применён частично: 1 из 2 шагов\nещё", mark="частично")
    ps = web.panel_state([partial], None, True, 3, _now())
    assert (ps.tone, ps.title) == ("сбой", "План применён частично: 1 из 2 шагов")
    assert web.panel_state([], None, False, 2, _now()).tone == "просмотр"
    assert web.panel_state([], status, False, 2, _now()).tone == "работа"   # чужое действие видно и в окне просмотра


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


# ---- 2.7: конец сессии (мониторинг, шаг 3) ----


def test_dialog_available_for_closed_session(tmp_runs_dir):
    """23.10: закрытая сессия чат не прячет; строка о закрытии говорит, как продолжить."""
    from app import sessions
    rid = "20261013_100000_aaaaaa"
    ready = _ready_run(rid)
    assert web.session_closed_text(rid) is None
    sessions.close_session(rid, by="таймер", now=datetime(2026, 10, 13, 12, 0))
    assert web.dialog_unavailable(rid, ready) is None
    assert web.session_closed_text(rid) == "Чтобы продолжить, напишите в чат."   # 27.10: только как продолжить


def test_sources_html_links_and_escapes():
    """Шаг 7 помощника: источники под ответом — адреса ссылками, остальное экранировано."""
    out = web.sources_html(["Вердикты, индикатор 3", "https://internet.garant.ru/#/document/70636256 — редакции <акта>"])
    assert 'href="https://internet.garant.ru/#/document/70636256"' in out and "&lt;акта&gt;" in out
    assert "Вердикты, индикатор 3" in out and web.sources_html([]) == ""


def test_start_hint_says_what_is_missing():
    """27.10 (удобство): под бледной «Проанализировать» — чего не хватает."""
    assert web.start_hint(False, False) == "Чтобы начать, загрузите файл акта и отметьте тип акта."
    assert web.start_hint(True, False) == "Чтобы начать, отметьте тип акта."
    assert web.start_hint(False, True) == "Чтобы начать, загрузите файл акта."
    assert web.start_hint(True, True) == ""


@pytest.mark.parametrize("path, section", [
    ("/", "/"), ("/history", "/history"), ("/run/20261023_141113_f26759", "/history"), ("/garant", "/garant"),
    ("/garant/abc123", "/garant"), ("/feedback", "/feedback"), ("/login", None),
])
def test_menu_section(path: str, section: str | None):
    """27.10: текущий раздел в меню; прогон — в «Истории», беседа — в «Беседах»."""
    assert web.menu_section(path) == section


def test_version_zero_reads_as_original():
    """27.10: «версия 0» эксперту непонятна — на панели «исходная версия»."""
    assert web.panel_state([], None, True, 0, _now()).detail == "Файл — исходная версия"
    assert web.version_label(2) == "версия 2"


def test_read_only_window_cannot_close_session_even_from_handler(monkeypatch):
    from types import SimpleNamespace
    from app.dialog_jobs import DialogLock

    lock = DialogLock(ttl_s=60)
    assert lock.acquire("run", "owner", web.time.monotonic())
    monkeypatch.setattr(web, "DIALOG_LOCK", lock)
    calls, notices = [], []
    monkeypatch.setattr(web, "close_by_user", lambda who, rid: calls.append(rid))
    monkeypatch.setattr(web.ui, "notify", lambda text, **kwargs: notices.append(text))
    monkeypatch.setattr(web.ui.navigate, "reload", lambda: None)

    view = web._RunView.__new__(web._RunView)
    view._run_id, view._who, view._requires_edit_lock = "run", None, True
    view._client_id = "viewer"
    box = SimpleNamespace(close=lambda: None)
    view._close(box)
    assert calls == [] and notices == [web.DIALOG_READ_ONLY]
    view._client_id = "owner"
    view._close(box)
    assert calls == ["run"]


def test_close_button_reflects_editing_lock(monkeypatch):
    from types import SimpleNamespace
    from app.dialog_jobs import DialogLock

    lock = DialogLock(ttl_s=60)
    lock.acquire("run", "first", web.time.monotonic())
    monkeypatch.setattr(web, "DIALOG_LOCK", lock)
    enabled = []
    view = web._RunView.__new__(web._RunView)
    view._run_id, view._requires_edit_lock = "run", True
    view._close_button = SimpleNamespace(enable=lambda: enabled.append(True), disable=lambda: enabled.append(False))
    view._client_id = "second"
    view._update_close_access()
    view._client_id = "first"
    view._update_close_access()
    assert enabled == [False, True]
