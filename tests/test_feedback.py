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
