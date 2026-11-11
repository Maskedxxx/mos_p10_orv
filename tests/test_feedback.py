"""
Тесты app/feedback.py — разбор заполненного экспертом Excel (план итерации 3, шаг 5).

Книга — настоящий выход build_excel на фикстурах test_excel; эксперт
«заполняет» оранжевые колонки через openpyxl, как в живых файлах второго круга.
"""

from __future__ import annotations

import pytest
from openpyxl import load_workbook

from app.feedback import _col, _find_header_row, is_queued, read_remarks, remark_message, summary
from app.nodes.excel import build_excel
from app.trace import run_dir
from test_excel import _buckets, _map, _passport, _request


@pytest.fixture
def filled(tmp_runs_dir, tmp_path):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_fb")
    wb = load_workbook(path)

    ws = wb["Вердикты"]
    hdr = _find_header_row(ws, "Индикатор")
    rows = {str(ws.cell(r, _col(ws, hdr, "Индикатор")).value).split(".")[0]: r for r in range(hdr + 1, ws.max_row + 1)}
    ws.cell(rows["1"], _col(ws, hdr, "Согласны?")).value = "нет"
    ws.cell(rows["1"], _col(ws, hdr, "Если нет — какой верный")).value = "соблюдён"
    ws.cell(rows["1"], _col(ws, hdr, "Замечание")).value = "взаимодействие через МВ есть"
    ws.cell(rows["2"], _col(ws, hdr, "Согласны?")).value = "да"

    ws = wb["Карта гипотез"]
    hdr = _find_header_row(ws)
    ws.cell(hdr + 1, _col(ws, hdr, "Оценка (1–5)")).value = 4
    ws.cell(hdr + 2, _col(ws, hdr, "Оценка (1–5)")).value = 2
    ws.cell(hdr + 2, _col(ws, hdr, "Замечание")).value = "гипотеза надуманная"

    ws = wb["Запрос показателей"]
    hdr = _find_header_row(ws, "Показатель")
    ws.cell(hdr + 1, _col(ws, hdr, "Оценка (1–5)")).value = 5
    # общее замечание под таблицей — как у эксперта по приютам во втором круге
    ws.cell(ws.max_row + 2, _col(ws, hdr, "Замечание")).value = "не хватает площади помещений"

    out = tmp_path / "эксперт.xlsx"
    wb.save(out)
    return out


def test_read_remarks_takes_only_filled_rows(filled):
    rs = read_remarks(filled)
    by = {(r.sheet, r.key): r for r in rs}
    assert len(rs) == 6
    assert by[("Запрос показателей", "")].text == "не хватает площади помещений"
    v1 = by[("Вердикты", "1")]
    assert (v1.title, v1.agree, v1.correct, v1.text) == ("межведомственное взаимодействие", "нет", "соблюдён",
                                                         "взаимодействие через МВ есть")
    assert by[("Вердикты", "2")].agree == "да" and by[("Вердикты", "2")].text == ""
    assert by[("Карта гипотез", "1")].score == 4 and by[("Карта гипотез", "1")].title == "гипотеза 1"
    assert by[("Карта гипотез", "2")].text == "гипотеза надуманная"
    assert by[("Запрос показателей", "1")].score == 5


def test_queue_summary_and_message(filled):
    rs = read_remarks(filled)
    queued = [r for r in rs if is_queued(r)]
    assert [(r.sheet, r.key) for r in queued] == [("Вердикты", "1"), ("Карта гипотез", "2"), ("Запрос показателей", "")]
    s = summary(rs)
    assert "согласен с 1" in s and "строк карты: 1" in s and "позиций запроса: 1" in s
    msg = remark_message(queued[0])
    assert "индикатор 1" in msg and "соблюдён" in msg and "взаимодействие через МВ есть" in msg
    assert "строка 2" in remark_message(queued[1]) and "2 из 5" in remark_message(queued[1])
    assert "общее замечание к листу Запрос показателей" in remark_message(queued[2])


def test_summary_skips_rows_without_number():
    from app.contracts import Remark
    rs = [Remark(sheet="Запрос показателей", key="", title="Перечни и реестры", score=5),
          Remark(sheet="Запрос показателей", key="1", title="показатель", score=3)]
    assert "позиций запроса: 1, средняя оценка 3,0" in summary(rs)


def test_not_a_result_workbook(tmp_path):
    from openpyxl import Workbook
    p = tmp_path / "чужой.xlsx"
    Workbook().save(p)
    with pytest.raises(ValueError, match="нет листа"):
        read_remarks(p)


def test_invalid_agreement_reports_exact_location(filled, tmp_path):
    wb = load_workbook(filled)
    ws = wb["Вердикты"]
    hdr = _find_header_row(ws, "Индикатор")
    row = hdr + 1
    col = _col(ws, hdr, "Согласны?")
    ws.cell(row, col).value = "ТЕСТ: прошу объяснить основание"
    out = tmp_path / "некорректный.xlsx"
    wb.save(out)
    with pytest.raises(ValueError) as exc:
        read_remarks(out)
    message = str(exc.value)
    assert "Вердикты" in message and f"строка {row}" in message
    assert ws.cell(row, col).coordinate in message and "Согласны?" in message
    assert "индикатор" in message and "да» или «нет" in message


@pytest.mark.parametrize("value", [0, 6, 2.5, "плохо", "3 балла"])
def test_invalid_score_reports_location(filled, tmp_path, value):
    wb = load_workbook(filled)
    ws = wb["Карта гипотез"]
    row, col = _find_header_row(ws) + 1, _col(ws, _find_header_row(ws), "Оценка (1–5)")
    ws.cell(row, col).value = value
    out = tmp_path / "ошибка.xlsx"
    wb.save(out)
    with pytest.raises(ValueError) as exc:
        read_remarks(out)
    assert "Карта гипотез" in str(exc.value) and ws.cell(row, col).coordinate in str(exc.value)
    assert "от 1 до 5" in str(exc.value)


def test_normalized_agreement_and_correct_alone_are_not_lost(filled, tmp_path):
    wb = load_workbook(filled)
    ws = wb["Вердикты"]
    hdr = _find_header_row(ws, "Индикатор")
    ws.cell(hdr + 1, _col(ws, hdr, "Согласны?")).value = "  НЕТ  "
    ws.cell(hdr + 2, _col(ws, hdr, "Согласны?")).value = "  ДА  "
    ws.cell(hdr + 3, _col(ws, hdr, "Если нет — какой верный")).value = "соблюдён"
    out = tmp_path / "допустимый.xlsx"
    wb.save(out)
    remarks = read_remarks(out)
    assert next(r for r in remarks if r.sheet == "Вердикты" and r.key == "1").agree == "нет"
    assert next(r for r in remarks if r.sheet == "Вердикты" and r.key == "2").agree == "да"
    assert is_queued(next(r for r in remarks if r.sheet == "Вердикты" and r.key == "3"))


def test_bad_upload_preserves_existing_queue_and_file(filled, tmp_runs_dir, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app import dialog, trace, web

    run_id = "run_upload"
    existing = read_remarks(filled)
    dialog.save_remarks(run_id, "эксперт.xlsx", existing)
    relname = "dialog/expert/эксперт.xlsx"
    trace.save_bytes(run_id, relname, filled.read_bytes())
    queue_before = (run_dir(run_id) / dialog.REMARKS).read_bytes()
    file_before = (run_dir(run_id) / relname).read_bytes()
    wb = load_workbook(filled)
    ws = wb["Вердикты"]
    hdr = _find_header_row(ws, "Индикатор")
    ws.cell(hdr + 1, _col(ws, hdr, "Согласны?")).value = "текст вместо ответа"
    from io import BytesIO
    bad = BytesIO()
    wb.save(bad)

    class Upload:
        name = "эксперт.xlsx"
        async def read(self):
            return bad.getvalue()

    notices = []
    monkeypatch.setattr(web.ui, "notify", lambda message, **kwargs: notices.append(message))
    monkeypatch.setattr(web.DIALOG_JOBS, "busy", lambda _: False)
    view = web._DialogView.__new__(web._DialogView)
    view._run_id, view._editable = run_id, True
    view._upload = SimpleNamespace(reset=lambda: None)
    asyncio.run(view._handle_expert_file(SimpleNamespace(file=Upload())))
    assert (run_dir(run_id) / dialog.REMARKS).read_bytes() == queue_before
    assert (run_dir(run_id) / relname).read_bytes() == file_before
    assert notices and "Согласны?" in notices[0] and "Вердикты" in notices[0]


# ---- 2.7 (итерация 4): «Где искать» и лист «Вопросы органу» ----


def test_where_to_search_becomes_recheck_message(tmp_runs_dir, tmp_path):
    """В: подсказка эксперта «Где искать» — в очередь, сообщением о перепроверке."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_fw")
    wb = load_workbook(path)
    ws = wb["Вердикты"]
    hdr = _find_header_row(ws, "Индикатор")
    row = next(r for r in range(hdr + 1, ws.max_row + 1) if str(ws.cell(r, 1).value).startswith("3."))
    ws.cell(row, _col(ws, hdr, "Где искать")).value = "постановление № 2384-ПП, п. 1.3.17"
    out = tmp_path / "эксперт.xlsx"
    wb.save(out)
    r = next(x for x in read_remarks(out) if x.key == "3")
    assert r.where_to_search == "постановление № 2384-ПП, п. 1.3.17" and is_queued(r)
    msg = remark_message(r)
    assert "эксперт подсказал, где искать: постановление № 2384-ПП, п. 1.3.17" in msg


def test_questions_sheet_remarks_read(tmp_runs_dir, tmp_path):
    from test_excel import _map_iter4, _request_iter4
    path = build_excel(_map_iter4(), _request_iter4(), _buckets(with_failed=False), _passport(), "run_fq")
    wb = load_workbook(path)
    ws = wb["Вопросы органу"]
    hdr = _find_header_row(ws, "Вопрос")
    row = next(r for r in range(hdr + 1, ws.max_row + 1) if ws.cell(r, 1).value == "2")
    ws.cell(row, _col(ws, hdr, "Оценка (1–5)")).value = 2
    ws.cell(row, _col(ws, hdr, "Замечание")).value = "спросить про внештатных экспертов"
    out = tmp_path / "эксперт.xlsx"
    wb.save(out)
    r = next(x for x in read_remarks(out) if x.sheet == "Вопросы органу")
    assert r.key == "2" and r.score == 2 and is_queued(r)
    assert "Вопросы органу, вопрос 2" in remark_message(r)


def test_old_book_without_questions_sheet_still_read(tmp_runs_dir, tmp_path):
    """Книги прогонов до 2.7 — без листа «Вопросы органу»: читаются как раньше."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_old")
    wb = load_workbook(path)
    del wb["Вопросы органу"]
    out = tmp_path / "старый.xlsx"
    wb.save(out)
    assert read_remarks(out) == []
