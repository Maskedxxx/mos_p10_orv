"""
Тесты app/nodes/excel.py (узел [7], детерминированная сборка .xlsx).

Модель не участвует, моков нет — реальный рендер через openpyxl (быстрый).
Проверяются: открываемость файла, состав листов, лист «Ошибки прогона» всегда
создаётся, три колонки годов (не четыре), разрезы подстроками, обратные
ссылки между листами «Карта гипотез»/«Запрос показателей» и оформление
(заливка шапки, freeze panes) — по образцу test_build_excel.py фазы 2.
"""

from __future__ import annotations

import datetime

from openpyxl import load_workbook

import app.nodes.excel as excel_module
from app.contracts import (
    Buckets,
    DataItem,
    DataRequest,
    Effect,
    HypothesisMap,
    MapRow,
    RequestItem,
    Verdict,
)
from app.nodes.excel import build_excel, compute_years
from app.trace import run_dir
import pytest


def _map() -> HypothesisMap:
    """Карта с двумя строками: одна с показателем «запрос органу» (уйдёт в
    запрос) и «открытый» рядом, вторая — целиком «запрос органу»."""
    return HypothesisMap(rows=[
        MapRow(
            row_id=1, indicators=[1, 8], hypothesis="гипотеза 1",
            effect=Effect(metric="издержки заявителя, руб.", how_to_calculate="сумма расходов"),
            data=[
                DataItem(metric="число заявок", source="запрос органу"),
                DataItem(metric="доля отказов", source="открытый", where="Мосстат"),
            ],
            proof="дословная цитата 1", clause="5.1.4",
        ),
        MapRow(
            row_id=2, indicators=[3], hypothesis="гипотеза 2",
            effect=Effect(metric="срок рассмотрения, дней", how_to_calculate="среднее по выборке"),
            data=[DataItem(metric="средний срок рассмотрения", source="запрос органу")],
        ),
    ], merges=[])


def _request() -> DataRequest:
    """Запрос: показатель «1» с двумя разрезами (для строки 1), показатель «2» без
    разрезов (для строки 2). years намеренно 4 — узел их игнорирует, считает сам."""
    return DataRequest(
        items=[
            RequestItem(
                num="1", metric="число заявок (пункт 5.1.4), ед.", clause="5.1.4",
                for_rows=[1], breakdown=["юридические лица", "индивидуальные предприниматели"],
            ),
            RequestItem(
                num="2", metric="средний срок рассмотрения, дней", for_rows=[2], breakdown=[],
            ),
        ],
        years=[2020, 2021, 2022, 2023],
    )


def _buckets(*, with_failed: bool) -> Buckets:
    checked = [
        Verdict(indicator=2, name="электронная подача документов", verdict="соблюдён",
                rationale="реализовано через портал госуслуг", proof="цитата", clause="2.1"),
        Verdict(indicator=5, name="возможность доработки пакета документов",
                verdict="не применимо", rationale="требование к пакету отсутствует"),
    ]
    failed = []
    if with_failed:
        failed = [
            Verdict(indicator=9, name="содержание понятных требований", verdict="ошибка",
                    rationale="таймаут процесса после 900 с", attempts=3),
        ]
    return Buckets(violated=[], no_data=[], checked=checked, failed=failed)


def _summary() -> str:
    """Реалистичное саммари с секцией «1. Реквизиты» — по образцу живого прогона
    (runs/20260824_154712_20b7d9/02_summary.md)."""
    return (
        "# Саммари нормативного правового акта\n\n"
        "## 1. Реквизиты\n\n"
        "- **Вид акта:** приказ\n"
        "- **Номер:** N 02-02-313/21\n"
        "- **Дата принятия:** 9 августа 2021 г.\n"
        "- **Принявший орган:** Департамент средств массовой информации и рекламы города Москвы\n"
        "- **Полное название:** «Об утверждении Порядка организации торгов»\n\n"
        "## 2. Что регулирует\n\nДалее текст саммари.\n"
    )


def _summary_unparseable() -> str:
    """Саммари без секции «1. Реквизиты» — реквизиты распознать нельзя (мусорный
    ввод: два первых markdown-заголовка без структуры полей)."""
    return "## Саммари\n\nПриказ ДСМИ от 09.08.2021 № 123 «О порядке».\n\nДалее текст саммари."


def _find_header_row(ws) -> int:
    """Найти строку заголовка листа «Карта гипотез» — она может быть сдвинута
    вниз строками саммари."""
    for i in range(1, ws.max_row + 1):
        if ws.cell(row=i, column=1).value == "Индикаторы":
            return i
    raise AssertionError("заголовок листа «Карта гипотез» не найден")


def test_opens_with_openpyxl_and_four_sheets(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _summary(), "run_a")
    wb = load_workbook(path)
    assert wb.sheetnames == [
        "Карта гипотез", "Запрос показателей", "Проверенные индикаторы", "Ошибки прогона",
    ]


def test_saved_as_08_result_in_run_dir(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_b")
    assert path == run_dir("run_b") / "08_result.xlsx"
    assert path.exists()


def test_failed_sheet_present_even_when_bucket_empty(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_c")
    ws = load_workbook(path)["Ошибки прогона"]
    assert [c.value for c in ws[1]] == ["Индикатор", "Что случилось", "Попыток"]
    assert ws.max_row == 2  # шапка + одна строка «ошибок нет»
    assert ws.cell(row=2, column=1).value == "ошибок нет"


def test_failed_sheet_lists_failures(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _summary(), "run_d")
    ws = load_workbook(path)["Ошибки прогона"]
    assert ws.max_row == 2
    assert "содержание понятных требований" in ws.cell(row=2, column=1).value
    assert ws.cell(row=2, column=2).value == "таймаут процесса после 900 с"
    assert ws.cell(row=2, column=3).value == 3


def test_checked_sheet_lists_verdicts(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_e")
    ws = load_workbook(path)["Проверенные индикаторы"]
    assert [c.value for c in ws[1]] == ["Индикатор", "Вердикт", "Обоснование", "Пруф", "Пункт"]
    assert ws.max_row == 3  # шапка + 2 вердикта
    assert "электронная подача документов" in ws.cell(row=2, column=1).value
    assert ws.cell(row=2, column=2).value == "соблюдён"


def test_request_sheet_has_three_year_columns_not_four(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_f")
    ws = load_workbook(path)["Запрос показателей"]
    header = [c.value for c in ws[1]]
    assert header[:2] == ["№", "Показатель"]
    assert len(header) == 5  # № + Показатель + 3 года
    assert len(header[2:]) == 3


def test_breakdown_rendered_as_subrows(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_g")
    ws = load_workbook(path)["Запрос показателей"]
    # строка 2: основной показатель "1", строки 3-4: подстроки "1.1"/"1.2"
    assert ws.cell(row=2, column=1).value == "1"
    assert ws.cell(row=3, column=1).value == "1.1"
    assert ws.cell(row=3, column=2).value == "юридические лица"
    assert ws.cell(row=4, column=1).value == "1.2"
    assert ws.cell(row=4, column=2).value == "индивидуальные предприниматели"
    # показатель "2" без разрезов идёт сразу за подстроками первого
    assert ws.cell(row=5, column=1).value == "2"
    assert ws.max_row == 5


def test_map_data_cell_backrefs_point_to_existing_request_rows(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_h")
    wb = load_workbook(path)
    ws_map = wb["Карта гипотез"]
    ws_req = wb["Запрос показателей"]

    header_row = _find_header_row(ws_map)
    data_row1 = ws_map.cell(row=header_row + 1, column=4).value  # колонка «Данные», строка карты 1
    data_row2 = ws_map.cell(row=header_row + 2, column=4).value

    assert "№ 1" in data_row1           # запрос органу -> ссылается на показатель № 1
    assert "открытый" in data_row1      # второй показатель без ссылки на запрос
    assert "№ 1" not in data_row1.split("открытый")[1]  # ссылка не приклеена к «открытому»
    assert "№ 2" in data_row2

    # номера "1" и "2" реально существуют на листе запроса (первая колонка)
    request_nums = {str(ws_req.cell(row=r, column=1).value) for r in range(2, ws_req.max_row + 1)}
    assert {"1", "2"} <= request_nums


def test_summary_header_has_requisites_not_raw_markdown(tmp_runs_dir):
    """Шапка — ОДНА осмысленная строка с вид/номер/дата/орган, а не первые
    попавшиеся markdown-заголовки саммари (правка владельца 2026-08-24: было
    «Саммари нормативного правового акта» / «1. Реквизиты» — мусор без данных)."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_i")
    wb = load_workbook(path)
    ws_map = wb["Карта гипотез"]

    header_row = _find_header_row(ws_map)
    assert header_row == 2  # ровно одна строка шапки перед заголовками колонок

    summary_cell = ws_map.cell(row=1, column=1).value
    assert summary_cell != "Индикаторы"
    assert "Саммари нормативного правового акта" not in summary_cell  # не сырой заголовок
    assert "Реквизиты" not in summary_cell                             # не обрывок оглавления
    assert "приказ" in summary_cell
    assert "N 02-02-313/21" in summary_cell
    assert "9 августа 2021 г." in summary_cell
    assert "Департамент средств массовой информации и рекламы города Москвы" in summary_cell

    for name in ("Карта гипотез", "Запрос показателей", "Проверенные индикаторы", "Ошибки прогона"):
        ws = wb[name]
        hdr = header_row if name == "Карта гипотез" else 1
        assert ws.cell(row=hdr, column=1).font.bold is True
        assert ws.cell(row=hdr, column=1).fill.fgColor.rgb.endswith("1F4E78")
        assert ws.freeze_panes == ws.cell(row=hdr + 1, column=1).coordinate


def test_no_summary_header_when_summary_empty(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_j")
    ws_map = load_workbook(path)["Карта гипотез"]
    assert ws_map.cell(row=1, column=1).value == "Индикаторы"


def test_no_summary_header_when_requisites_unrecognizable(tmp_runs_dir):
    """Саммари непустое, но без секции «1. Реквизиты» / узнаваемых полей —
    шапки нет вовсе, лист начинается сразу с заголовков колонок, мусорных
    строк нет (требование владельца 2026-08-24: мусорная шапка хуже отсутствующей)."""
    path = build_excel(
        _map(), _request(), _buckets(with_failed=False), _summary_unparseable(), "run_j2",
    )
    ws_map = load_workbook(path)["Карта гипотез"]
    assert ws_map.cell(row=1, column=1).value == "Индикаторы"
    assert ws_map.max_row == 3  # заголовок + 2 строки карты, ни одной мусорной сверху


def test_map_sheet_row_content(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_k")
    ws = load_workbook(path)["Карта гипотез"]
    assert [c.value for c in ws[1]] == ["Индикаторы", "Гипотеза", "Эффект", "Данные", "Пруф", "Пункт"]
    assert ws.cell(row=2, column=1).value == "1, 8"
    assert ws.cell(row=2, column=2).value == "гипотеза 1"
    assert "издержки заявителя" in ws.cell(row=2, column=3).value
    assert "сумма расходов" in ws.cell(row=2, column=3).value
    assert ws.cell(row=2, column=5).value == "дословная цитата 1"
    assert ws.cell(row=2, column=6).value == "5.1.4"


def test_compute_years_excludes_current_incomplete_year(monkeypatch):
    """Правка владельца 2026-08-24: текущий (незавершённый) год не должен
    попадать в бланк запроса — при любой дате внутри года результат один и
    тот же: три полных ПРЕДШЕСТВУЮЩИХ года, без текущего."""

    class _FakeDateNewYear(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 1, 1)

    class _FakeDateYearEnd(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 12, 31)

    monkeypatch.setattr(excel_module.datetime, "date", _FakeDateNewYear)
    assert compute_years(3) == [2023, 2024, 2025]

    monkeypatch.setattr(excel_module.datetime, "date", _FakeDateYearEnd)
    assert compute_years(3) == [2023, 2024, 2025]


def test_compute_years_explicit_end_year_still_literal():
    """Явно переданный end_year уважается как есть — поправка касается только
    автоопределения текущей даты, не меняет поведение при заданном годе."""
    assert compute_years(4, 2026) == [2023, 2024, 2025, 2026]
    assert compute_years(1, 2026) == [2026]

# ----------------------------------------------------------------------
# Заголовок секции реквизитов: промпт саммари фиксирует смысл раздела,
# но не точную формулировку. На живом прогоне 2026-08-25 модель написала
# "## 1. Реквизиты акта" — строгий якорь секцию не нашёл, и лист карты
# вышел без шапки. Матчим нестрого и проверяем варианты.
# ----------------------------------------------------------------------

_REQUISITES_BODY = """
- **Вид акта:** Постановление Правительства Москвы
- **Номер:** N 100-ПП
- **Дата принятия:** 25.02.2013
- **Принявший орган:** Правительство Москвы
- **Полное название:** «О реализации пилотного проекта "Доктор рядом"»

## 2. Что регулирует

Текст.
"""


@pytest.mark.parametrize("heading", [
    "## 1. Реквизиты",
    "## 1. Реквизиты акта",
    "## Реквизиты акта",
    "### Реквизиты",
    "## 1 Реквизиты",
])
def test_summary_header_found_for_heading_variants(heading):
    """Лишнее слово или иной уровень заголовка не должны лишать лист шапки."""
    from app.nodes.excel import _summary_header_line

    line = _summary_header_line(f"# Саммари\n\n{heading}\n{_REQUISITES_BODY}")
    assert line is not None, f"секция не найдена при заголовке «{heading}»"
    assert "100-ПП" in line and "25.02.2013" in line


def test_summary_header_absent_when_requisites_missing():
    """Без секции реквизитов шапки нет — мусорная хуже отсутствующей."""
    from app.nodes.excel import _summary_header_line

    assert _summary_header_line("# Саммари\n\n## 2. Что регулирует\n\nТекст.") is None
