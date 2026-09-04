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
    SourceRef,
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
                # намеренно вперемешку: открытый первым — рендер обязан
                # поднять «запрос органу» наверх сам (фикс N 2)
                DataItem(metric="доля отказов", source="открытый", where="Мосстат"),
                DataItem(metric="число заявок", source="запрос органу"),
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
    # индикаторы 1 и 8 — строка карты 1, индикатор 3 — строка карты 2
    violated = [
        # цитаты нет, вывод построен на внешней практике (случай индикатора 10
        # живого прогона); пункт вердикта «9.9» намеренно расходится с пунктом
        # строки карты «5.1.4» — карта не должна подменяться вердиктом
        Verdict(indicator=1, name="межведомственное взаимодействие", verdict="нарушен",
                rationale="практика показывает обратное", clause="9.9",
                sources=["https://practice.example/1", "https://practice.example/2"]),
        Verdict(indicator=8, name="оптимальный пакет документов", verdict="нарушен",
                rationale="пакет избыточен", proof="цитата вердикта 8", clause="8.8",
                sources=["https://practice.example/1"]),   # дубль адреса — дедупликация
    ]
    no_data = [
        # у вердикта цитата ЕСТЬ, а строка карты 2 идёт без неё — подстановка
        Verdict(indicator=3, name="актуальность законодательства", verdict="нет данных",
                rationale="статистики нет", proof="цитата вердикта 3", clause="2.5",
                missing_data="число обращений за 3 года"),
    ]
    checked = [
        Verdict(indicator=2, name="электронная подача документов", verdict="соблюдён",
                rationale="реализовано через портал госуслуг", proof="цитата", clause="2.1",
                sources=["https://docs.cntd.ru/document/3666532", "https://www.mos.ru/depr/"]),
        Verdict(indicator=5, name="возможность доработки пакета документов",
                verdict="не применимо", rationale="требование к пакету отсутствует"),
    ]
    failed = []
    if with_failed:
        failed = [
            Verdict(indicator=9, name="содержание понятных требований", verdict="ошибка",
                    rationale="таймаут процесса после 900 с", attempts=3),
        ]
    return Buckets(violated=violated, no_data=no_data, checked=checked, failed=failed)


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


def _find_header_row(ws, anchor: str = "Индикаторы") -> int:
    """
    Найти строку шапки листа по известному заголовку в любой колонке.

    Привязываться к координатам нельзя: строка шапки едет вниз (строка
    реквизитов акта, подпись листа), а колонки едут вправо (добавляется «№»).
    Ищем сам заголовок.
    """
    for i in range(1, ws.max_row + 1):
        for c in ws[i]:
            if c.value == anchor:
                return i
    raise AssertionError(f"строка шапки с заголовком «{anchor}» не найдена")


def _col(ws, header_row: int, name: str) -> int:
    """Номер колонки по тексту её заголовка в строке шапки."""
    for c in ws[header_row]:
        if c.value == name:
            return c.column
    raise AssertionError(f"колонка «{name}» не найдена в строке {header_row}")


def test_opens_with_openpyxl_and_four_sheets(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _summary(), "run_a")
    wb = load_workbook(path)
    assert wb.sheetnames == [
        "Карта гипотез", "Запрос показателей", "Нарушений не найдено", "Ошибки прогона",
    ]


def test_saved_as_08_result_in_run_dir(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_b")
    assert path == run_dir("run_b") / "08_result.xlsx"
    assert path.exists()


def test_failed_sheet_present_even_when_bucket_empty(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_c")
    ws = load_workbook(path)["Ошибки прогона"]
    hdr = _find_header_row(ws, "Что случилось")
    assert [c.value for c in ws[hdr]] == ["Индикатор", "Что случилось", "Попыток"]
    assert ws.max_row == hdr + 1  # шапка + одна строка «ошибок нет»
    assert ws.cell(row=hdr + 1, column=1).value == "ошибок нет"


def test_failed_sheet_lists_failures(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _summary(), "run_d")
    ws = load_workbook(path)["Ошибки прогона"]
    hdr = _find_header_row(ws, "Что случилось")
    row = hdr + 1
    assert ws.max_row == row
    assert "содержание понятных требований" in ws.cell(row=row, column=_col(ws, hdr, "Индикатор")).value
    assert ws.cell(row=row, column=_col(ws, hdr, "Что случилось")).value == "таймаут процесса после 900 с"
    assert ws.cell(row=row, column=_col(ws, hdr, "Попыток")).value == 3


def test_checked_sheet_lists_verdicts(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_e")
    ws = load_workbook(path)["Нарушений не найдено"]
    hdr = _find_header_row(ws, "Вердикт")
    assert [c.value for c in ws[hdr]] == [
        "Индикатор", "Вердикт", "Обоснование", "На чём основано",
    ]
    assert ws.max_row == hdr + 2  # шапка + 2 вердикта
    assert "электронная подача документов" in ws.cell(row=hdr + 1, column=_col(ws, hdr, "Индикатор")).value
    assert ws.cell(row=hdr + 1, column=_col(ws, hdr, "Вердикт")).value == "соблюдён"


def test_request_sheet_has_three_year_columns_not_four(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_f")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, "Показатель")
    header = [c.value for c in ws[hdr]]
    assert header[:2] == ["№", "Показатель"]
    # колонки годов ищем по виду заголовка, а не по позиции: между «Показателем»
    # и годами могут появиться колонки связи с картой
    years = [h for h in header if h and str(h).isdigit() and len(str(h)) == 4]
    assert len(years) == 3


def test_breakdown_rendered_as_subrows(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_g")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, "Показатель")
    num, metric = _col(ws, hdr, "№"), _col(ws, hdr, "Показатель")
    # первая строка после шапки: основной показатель "1", затем подстроки "1.1"/"1.2"
    assert ws.cell(row=hdr + 1, column=num).value == "1"
    assert ws.cell(row=hdr + 2, column=num).value == "1.1"
    assert ws.cell(row=hdr + 2, column=metric).value == "юридические лица"
    assert ws.cell(row=hdr + 3, column=num).value == "1.2"
    assert ws.cell(row=hdr + 3, column=metric).value == "индивидуальные предприниматели"
    # показатель "2" без разрезов идёт сразу за подстроками первого
    assert ws.cell(row=hdr + 4, column=num).value == "2"
    assert ws.max_row == hdr + 4


def test_map_data_cell_backrefs_point_to_existing_request_rows(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_h")
    wb = load_workbook(path)
    ws_map = wb["Карта гипотез"]
    ws_req = wb["Запрос показателей"]

    header_row = _find_header_row(ws_map)
    data_col = _col(ws_map, header_row, "Данные")
    data_row1 = ws_map.cell(row=header_row + 1, column=data_col).value  # строка карты 1
    data_row2 = ws_map.cell(row=header_row + 2, column=data_col).value

    assert "№ 1" in data_row1           # запрос органу -> ссылается на показатель № 1
    assert "Открытые источники" in data_row1   # открытый показатель в своём блоке
    # ссылка стоит в заголовке блока запроса и не приклеена к открытому блоку
    assert "№" not in data_row1.split("Открытые источники")[1]
    assert "№ 2" in data_row2

    # номера "1" и "2" реально существуют на листе запроса (первая колонка)
    req_hdr = _find_header_row(ws_req, "Показатель")
    req_num_col = _col(ws_req, req_hdr, "№")
    request_nums = {
        str(ws_req.cell(row=r, column=req_num_col).value)
        for r in range(req_hdr + 1, ws_req.max_row + 1)
    }
    assert {"1", "2"} <= request_nums


def test_summary_header_has_requisites_not_raw_markdown(tmp_runs_dir):
    """Шапка — ОДНА осмысленная строка с вид/номер/дата/орган, а не первые
    попавшиеся markdown-заголовки саммари (правка владельца 2026-08-24: было
    «Саммари нормативного правового акта» / «1. Реквизиты» — мусор без данных)."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_i")
    wb = load_workbook(path)
    ws_map = wb["Карта гипотез"]

    header_row = _find_header_row(ws_map)
    assert header_row == 3  # реквизиты + подпись листа перед заголовками колонок

    summary_cell = ws_map.cell(row=1, column=1).value
    assert summary_cell != "Индикаторы"
    assert "Саммари нормативного правового акта" not in summary_cell  # не сырой заголовок
    assert "Реквизиты" not in summary_cell                             # не обрывок оглавления
    assert "приказ" in summary_cell
    assert "N 02-02-313/21" in summary_cell
    assert "9 августа 2021 г." in summary_cell
    assert "Департамент средств массовой информации и рекламы города Москвы" in summary_cell

    for name, anchor in (
        ("Карта гипотез", "Индикаторы"),
        ("Запрос показателей", "Показатель"),
        ("Нарушений не найдено", "Вердикт"),
        ("Ошибки прогона", "Что случилось"),
    ):
        ws = wb[name]
        hdr = _find_header_row(ws, anchor)
        assert ws.cell(row=hdr, column=1).font.bold is True
        assert ws.cell(row=hdr, column=1).fill.fgColor.rgb.endswith("1F4E78")
        assert ws.freeze_panes == ws.cell(row=hdr + 1, column=1).coordinate


def test_no_summary_header_when_summary_empty(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_j")
    ws_map = load_workbook(path)["Карта гипотез"]
    assert _find_header_row(ws_map) == 2   # только подпись листа, строки реквизитов нет


def test_no_summary_header_when_requisites_unrecognizable(tmp_runs_dir):
    """Саммари непустое, но без секции «1. Реквизиты» / узнаваемых полей —
    шапки нет вовсе, лист начинается сразу с заголовков колонок, мусорных
    строк нет (требование владельца 2026-08-24: мусорная шапка хуже отсутствующей)."""
    path = build_excel(
        _map(), _request(), _buckets(with_failed=False), _summary_unparseable(), "run_j2",
    )
    ws_map = load_workbook(path)["Карта гипотез"]
    assert _find_header_row(ws_map) == 2                   # подпись, мусорной строки нет
    assert ws_map.max_row == 4                             # подпись + шапка + 2 строки карты


def test_map_sheet_row_content(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_k")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    assert [c.value for c in ws[hdr]] == [
        "№", "Индикаторы", "Гипотеза", "Эффект", "Данные", "На чём основано",
    ]
    row = hdr + 1
    assert ws.cell(row=row, column=_col(ws, hdr, "Индикаторы")).value == (
        "1. межведомственное взаимодействие\n8. оптимальный пакет документов"
    )
    assert ws.cell(row=row, column=_col(ws, hdr, "Гипотеза")).value == "гипотеза 1"
    effect = ws.cell(row=row, column=_col(ws, hdr, "Эффект")).value
    assert "издержки заявителя" in effect
    assert "сумма расходов" in effect
    basis = ws.cell(row=row, column=_col(ws, hdr, "На чём основано")).value
    assert "«дословная цитата 1»" in basis
    assert "п. 5.1.4" in basis


# ----------------------------------------------------------------------
# Фикс N 1 (трек багов, 2026-09-02): в карте гипотез у индикаторов не было
# имён — только номера. Владелец: «в индикаторов нет имён индикаторов, а в
# Проверенные индикаторы есть. Надо чтобы везде имена были».
# ----------------------------------------------------------------------


def test_map_indicator_column_has_names(tmp_runs_dir):
    """Номер и имя, как на листе проверенных индикаторов, а не голая цифра."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_ind_names")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    col = _col(ws, hdr, "Индикаторы")

    # строка карты 1 объединяет индикаторы 1 и 8 — каждый на своей строке клетки
    assert ws.cell(row=hdr + 1, column=col).value == (
        "1. межведомственное взаимодействие\n8. оптимальный пакет документов"
    )
    # строка карты 2 — один индикатор
    assert ws.cell(row=hdr + 2, column=col).value == "3. актуальность законодательства"


def test_map_indicator_column_wide_enough_for_names(tmp_runs_dir):
    """Ширина колонки под имена — как на листе проверенных индикаторов (34)."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_ind_width")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    letter = ws.cell(row=hdr, column=_col(ws, hdr, "Индикаторы")).column_letter
    assert ws.column_dimensions[letter].width >= 34


# ----------------------------------------------------------------------
# Фикс N 4 (трек багов, 2026-09-02): в колонке «Эффект» показатель и способ
# его расчёта шли подряд без подписей — глазом не видно, где кончается «что
# меряем» и начинается «как считать».
# ----------------------------------------------------------------------


def test_effect_cell_has_labels(tmp_runs_dir):
    """Два блока подписаны и разделены пустой строкой."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_effect")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    cell = ws.cell(row=hdr + 1, column=_col(ws, hdr, "Эффект")).value

    assert cell == (
        "Показатель:\n"
        "издержки заявителя, руб.\n"
        "\n"
        "Как считать:\n"
        "сумма расходов"
    )


# ----------------------------------------------------------------------
# Фиксы N 2 и N 3 (трек багов, 2026-09-02): колонка «Данные» шла сплошным
# текстом, тип источника был спрятан в скобках в середине строки, а номера
# строк запроса приписывались КАЖДОМУ показателю целым списком («N 1, 2»
# у обоих), из-за чего сноска переставала указывать на конкретную строку.
#
# Проверено на прогоне 20260825_093039_129668: сопоставить показатель карты
# со строкой запроса по тексту невозможно — 0 точных совпадений из 20, модель
# узла [6] переформулирует. Поэтому номера выносятся в ЗАГОЛОВОК блока: это
# ровно та точность, что есть в данных (RequestItem.for_rows -> MapRow.row_id).
# ----------------------------------------------------------------------


def _data_cell(path, row_offset: int = 1) -> str:
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    return ws.cell(row=hdr + row_offset, column=_col(ws, hdr, "Данные")).value


def test_data_cell_groups_by_source(tmp_runs_dir):
    """«Запрос в орган» выше «Открытых источников», хотя в данных порядок обратный."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_data_group")
    cell = _data_cell(path)
    assert cell.index("Запрос в орган") < cell.index("Открытые источники")
    assert cell.index("число заявок") < cell.index("доля отказов")


def test_data_cell_open_sources_annotated(tmp_runs_dir):
    """У открытого блока есть пояснение, что у органа это не запрашивают."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_data_annot")
    cell = _data_cell(path)
    assert "Открытые источники (берём сами, у органа не запрашиваем):" in cell
    assert "Мосстат" in cell           # адрес открытого источника сохранён
    assert "(открытый" not in cell     # старая пометка в скобках убрана


def test_data_cell_request_nums_on_block_not_items(tmp_runs_dir):
    """Номер строки запроса стоит один раз в заголовке блока, а не у показателя."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_data_nums")
    cell = _data_cell(path)
    lines = cell.splitlines()

    head = [ln for ln in lines if ln.startswith("Запрос в орган")]
    assert head == ["Запрос в орган (лист «Запрос показателей», № 1):"]
    # ни одна строка-показатель номера не несёт
    assert [ln for ln in lines if ln.startswith("—") and "№" in ln] == []
    assert cell.count("№") == 1


def test_data_cell_omits_empty_group(tmp_runs_dir):
    """Строка без открытых показателей не печатает пустой заголовок группы."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_data_empty")
    cell = _data_cell(path, row_offset=2)   # строка карты 2 — только «запрос органу»
    assert "Запрос в орган" in cell
    assert "Открытые источники" not in cell


# ----------------------------------------------------------------------
# Фикс N 5 (трек багов, 2026-09-02): из документа не было понятно, откуда
# взят пруф. Две мини-правки: заголовки «Пруф»/«Пункт» сводятся в одну
# понятную колонку, и в неё возвращаются внешние источники, которые модель
# находила, но которые терялись между шагом [2] и шагом [5] — ни Hypothesis,
# ни MapRow поля sources не имеют.
# ----------------------------------------------------------------------


def _basis_cell(path, row_offset: int = 1) -> str:
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    return ws.cell(row=hdr + row_offset, column=_col(ws, hdr, "На чём основано")).value


def test_basis_column_replaces_proof_and_clause(tmp_runs_dir):
    """Вместо «Пруф» и «Пункт» одна колонка «На чём основано»."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_basis_hdr")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    headers = [c.value for c in ws[hdr]]
    assert "На чём основано" in headers
    assert "Пруф" not in headers
    assert "Пункт" not in headers


def test_basis_cell_has_norm_label(tmp_runs_dir):
    """Цитата подписана как норма акта, с номером пункта из строки карты."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_basis_norm")
    cell = _basis_cell(path)
    assert cell.startswith("Норма акта, п. 5.1.4:\n«дословная цитата 1»")


def test_basis_clause_not_overridden_by_verdict(tmp_runs_dir):
    """Пункт берётся из строки карты (5.1.4), а не из вердикта (9.9)."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_basis_clause")
    cell = _basis_cell(path)
    assert "п. 5.1.4" in cell
    assert "9.9" not in cell


def test_basis_cell_carries_web_sources(tmp_runs_dir):
    """Адреса из вердиктов индикаторов строки доезжают до документа, без дублей."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_basis_src")
    cell = _basis_cell(path)
    assert "Внешние источники:" in cell
    assert "— https://practice.example/1" in cell
    assert "— https://practice.example/2" in cell
    assert cell.count("https://practice.example/1") == 1   # индикаторы 1 и 8 дали дубль


def test_basis_cell_falls_back_to_verdict_proof(tmp_runs_dir):
    """Строка карты без цитаты берёт цитату вердикта своего индикатора."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_basis_fb")
    cell = _basis_cell(path, row_offset=2)   # строка карты 2: proof=None, clause=None
    assert "«цитата вердикта 3»" in cell
    assert cell.startswith("Норма акта:")    # пункта у строки карты нет — и не подставляем


def test_basis_cell_states_when_no_quote_but_sources():
    """Нет цитаты, но есть адреса — пишем прямо, а не оставляем пустоту."""
    from app.nodes.excel import _render_basis_cell

    cell = _render_basis_cell(None, "п. 2.2, 2.3, 4-6", ["https://www.privatmed.ru/a"])
    assert cell.startswith("Норма акта, п. 2.2, 2.3, 4-6:")   # префикс «п.» не удваивается
    assert "цитата не приведена" in cell
    assert "внешних источниках" in cell
    assert "— https://www.privatmed.ru/a" in cell


def test_basis_cell_states_when_nothing_at_all():
    """Ни цитаты, ни адресов — тоже словами, а не пустой клеткой."""
    from app.nodes.excel import _render_basis_cell

    assert _render_basis_cell(None, None, []) == "Норма акта:\nцитата из акта не приведена"


# ----------------------------------------------------------------------
# Фикс N 7.2 (трек багов, 2026-09-02): на листе проверенных индикаторов те же
# «Пруф» и «Пункт» с тем же дефектом — адреса терялись. У индикатора 3 живого
# прогона модель нашла шесть адресов, в файле не было ни одного.
# ----------------------------------------------------------------------


def test_checked_sheet_basis_column(tmp_runs_dir):
    """Та же колонка «На чём основано», что и в карте, с теми же блоками."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_chk_basis")
    ws = load_workbook(path)["Нарушений не найдено"]
    hdr = _find_header_row(ws, "Вердикт")

    assert [c.value for c in ws[hdr]] == [
        "Индикатор", "Вердикт", "Обоснование", "На чём основано",
    ]

    basis = ws.cell(row=hdr + 1, column=_col(ws, hdr, "На чём основано")).value
    assert basis.startswith("Норма акта, п. 2.1:\n«цитата»")
    assert "Внешние источники:" in basis
    assert "— https://docs.cntd.ru/document/3666532" in basis
    assert "— https://www.mos.ru/depr/" in basis

    # индикатор 5 «не применимо» — ни цитаты, ни адресов, но клетка не пустая
    assert ws.cell(row=hdr + 2, column=_col(ws, hdr, "На чём основано")).value == (
        "Норма акта:\nцитата из акта не приведена"
    )


# ----------------------------------------------------------------------
# Фикс N 6 (трек багов, 2026-09-02): связь листов была односторонней. Из карты
# в запрос номер вёл, обратно — никак: получив от органа цифры, аналитик не
# видел, к какой гипотезе они относятся. Данные для связи уже посчитаны
# (RequestItem.for_rows, RequestItem.clause, MapRow.row_id) — их не выводили.
# ----------------------------------------------------------------------


def test_map_sheet_has_row_number_column(tmp_runs_dir):
    """Первая колонка карты — номер строки, тот самый, на который ссылается запрос."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_rownum")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)

    assert [c.value for c in ws[hdr]] == [
        "№", "Индикаторы", "Гипотеза", "Эффект", "Данные", "На чём основано",
    ]
    num = _col(ws, hdr, "№")
    assert ws.cell(row=hdr + 1, column=num).value == 1
    assert ws.cell(row=hdr + 2, column=num).value == 2


def test_request_sheet_shows_source_rows(tmp_runs_dir):
    """Показатель, обслуживающий две строки карты, показывает обе с индикаторами."""
    request = DataRequest(
        items=[RequestItem(num="1", metric="число заявок", clause="4",
                           for_rows=[1, 2], breakdown=[])],
        years=[2020, 2021, 2022],
    )
    path = build_excel(_map(), request, _buckets(with_failed=False), "", "run_req_src")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, "Показатель")

    assert ws.cell(row=hdr + 1, column=_col(ws, hdr, "Откуда запрос")).value == (
        "стр. 1 — инд. 1, 8\nстр. 2 — инд. 3"
    )


def test_request_sheet_shows_clause(tmp_runs_dir):
    """Пункт акта из RequestItem.clause выводится своей колонкой."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_req_clause")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, "Показатель")
    assert ws.cell(row=hdr + 1, column=_col(ws, hdr, "Пункт акта")).value == "5.1.4"
    # у показателя «2» пункта нет — клетка пустая, а не «None»
    assert ws.cell(row=hdr + 4, column=_col(ws, hdr, "Пункт акта")).value in (None, "")


def test_request_breakdown_subrows_have_no_backref(tmp_runs_dir):
    """Подстроки разрезов ссылок не несут — они принадлежат родительской строке."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_req_sub")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, "Показатель")
    src = _col(ws, hdr, "Откуда запрос")
    assert ws.cell(row=hdr + 2, column=_col(ws, hdr, "№")).value == "1.1"
    assert ws.cell(row=hdr + 2, column=src).value in (None, "")


def test_backref_round_trip(tmp_runs_dir):
    """Из карты в запрос и обратно — оба номера ведут на существующие строки."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), "", "run_roundtrip")
    wb = load_workbook(path)
    ws_map, ws_req = wb["Карта гипотез"], wb["Запрос показателей"]

    map_hdr = _find_header_row(ws_map)
    req_hdr = _find_header_row(ws_req, "Показатель")

    map_ids = {
        ws_map.cell(row=r, column=_col(ws_map, map_hdr, "№")).value
        for r in range(map_hdr + 1, ws_map.max_row + 1)
    }
    req_nums = {
        str(ws_req.cell(row=r, column=_col(ws_req, req_hdr, "№")).value)
        for r in range(req_hdr + 1, ws_req.max_row + 1)
    }

    # туда: номер из блока «Запрос в орган» строки карты 1 есть на листе запроса
    assert "№ 1" in ws_map.cell(row=map_hdr + 1, column=_col(ws_map, map_hdr, "Данные")).value
    assert "1" in req_nums

    # обратно: номер строки из «Откуда запрос» есть среди номеров карты
    back = ws_req.cell(row=req_hdr + 1, column=_col(ws_req, req_hdr, "Откуда запрос")).value
    assert back.startswith("стр. 1 — инд. ")
    assert 1 in map_ids


# ----------------------------------------------------------------------
# Фикс N 7.1 (трек багов, 2026-09-02): из имени листа не понять принцип
# отбора. «Проверенные индикаторы» не говорит, что здесь как раз те, по
# которым гипотезы НЕ строились — владелец, глядя на готовый файл, не понял,
# зачем этот лист нужен. В имя листа много не влезет (Excel режет на 31
# знаке), поэтому смысл выносится первой строкой листа.
# ----------------------------------------------------------------------

_SHEETS_AND_ANCHORS = (
    ("Карта гипотез", "Индикаторы"),
    ("Запрос показателей", "Показатель"),
    ("Нарушений не найдено", "Вердикт"),
    ("Ошибки прогона", "Что случилось"),
)


def test_sheet_names_say_what_is_on_them(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _summary(), "run_names")
    wb = load_workbook(path)
    assert wb.sheetnames == [name for name, _ in _SHEETS_AND_ANCHORS]


def test_each_sheet_has_caption_above_header(tmp_runs_dir):
    """На каждом листе есть строка-подпись, шапка колонок сдвинута под неё."""
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _summary(), "run_caption")
    wb = load_workbook(path)
    for name, anchor in _SHEETS_AND_ANCHORS:
        ws = wb[name]
        hdr = _find_header_row(ws, anchor)
        caption = ws.cell(row=hdr - 1, column=1).value
        assert caption and len(caption) > 30, f"нет подписи на листе «{name}»"
        assert ws.cell(row=hdr - 1, column=1).font.italic is True


def test_map_sheet_keeps_requisites_above_caption(tmp_runs_dir):
    """Реквизиты акта остаются самой первой строкой, подпись — под ними."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_cap_req")
    ws = load_workbook(path)["Карта гипотез"]
    assert _find_header_row(ws) == 3            # реквизиты + подпись + шапка
    assert "N 02-02-313/21" in ws.cell(row=1, column=1).value
    assert ws.cell(row=2, column=1).value.startswith("Индикаторы, по которым найдено нарушение")


def test_caption_counts_come_from_data_not_hardcoded(tmp_runs_dir):
    """Числитель считается по факту прогона, знаменатель — из конфига."""
    from app.config import all_indicators

    total = len(all_indicators())
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _summary(), "run_cap_cnt")
    wb = load_workbook(path)

    # в карте две строки: индикаторы 1, 8 и 3 — три уникальных
    assert f"3 из {total}" in wb["Карта гипотез"].cell(row=2, column=1).value
    # проверенных в куче два
    assert f"2 из {total}" in wb["Нарушений не найдено"].cell(row=1, column=1).value


def test_caption_does_not_break_header_styling(tmp_runs_dir):
    """Заливка шапки и freeze panes встали на сдвинутую строку."""
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _summary(), "run_cap_style")
    wb = load_workbook(path)
    for name, anchor in _SHEETS_AND_ANCHORS:
        ws = wb[name]
        hdr = _find_header_row(ws, anchor)
        assert ws.cell(row=hdr, column=1).font.bold is True
        assert ws.cell(row=hdr, column=1).fill.fgColor.rgb.endswith("1F4E78")
        assert ws.freeze_panes == ws.cell(row=hdr + 1, column=1).coordinate
        # подпись заливки шапки не получила
        assert ws.cell(row=hdr - 1, column=1).fill.fgColor.rgb != "FF1F4E78"


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


def _buckets_with_unverified() -> Buckets:
    """Как _buckets(with_failed=False), плюс «нет данных» по индикатору 4 БЕЗ строки
    в карте — так бывает, когда узел [4б] для индикатора не вызывался (семейство
    «текст / правовая база», Б1) либо вернул пустой список гипотез."""
    b = _buckets(with_failed=False)
    b.no_data.append(
        Verdict(indicator=4, name="отсутствие дублирования требований", verdict="нет данных",
                rationale="сопоставить требования п. 2.6 с Законом № 27 не удалось: текст закона не открылся",
                proof="цитата вердикта 4", clause="2.6",
                missing_data="текст статей Закона города Москвы от 13.11.2024 № 27"),
    )
    return b


def test_map_has_unverified_row_for_no_data_without_hypotheses(tmp_runs_dir):
    """Б1. Вердикт «нет данных» без гипотез раньше не попадал ни на один лист —
    аналитик не узнавал, что проверка не завершена. Теперь это строка карты:
    что не проверено и что в запрос органу не идёт."""
    path = build_excel(_map(), _request(), _buckets_with_unverified(), _summary(), "run_unverified")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    rows = list(ws.iter_rows(min_row=hdr + 1, values_only=True))
    assert len(rows) == 3, "две строки карты + одна строка незавершённой проверки"

    num, ind, hyp, effect, data, basis = rows[2][:6]
    assert num == 3, "нумерация продолжается за строками карты"
    assert ind == "4. отсутствие дублирования требований"
    assert "Проверка не завершена" in hyp
    assert "текст статей Закона города Москвы от 13.11.2024 № 27" in hyp, "что не проверено — из missing_data"
    assert "запрос органу не идёт" in data
    assert "цитата вердикта 4" in basis and "2.6" in basis


def test_map_caption_counts_unverified_indicators(tmp_runs_dir):
    """Б1. Подпись карты обещает «нарушено или не хватило данных — N из 12»:
    незавершённая проверка входит в N."""
    path = build_excel(_map(), _request(), _buckets_with_unverified(), _summary(), "run_unverified_cap")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    caption = str(ws.cell(row=hdr - 1, column=1).value)
    assert "4 из 12" in caption, caption   # индикаторы 1, 8, 3 из карты + 4 незавершённый


def test_map_no_unverified_row_when_no_data_has_hypotheses(tmp_runs_dir):
    """Б1, граница: «нет данных» по индикатору 3 уже представлен строкой карты 2 —
    второй строки для него не появляется."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_no_dup")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    rows = list(ws.iter_rows(min_row=hdr + 1, values_only=True))
    assert len(rows) == 2


# ---- ОС экспертов 2026-09-04, узел 6: Г1 единица в бланке, Г2 перечни отдельным блоком ----


def _request_v2() -> DataRequest:
    return DataRequest(
        items=[
            RequestItem(num="1", kind="показатель по годам", metric="число заявок (пункт 5.1.4)", unit="ед.",
                        clause="5.1.4", for_rows=[1], sources=[SourceRef(row_id=1, metric="число заявок")]),
            RequestItem(num="2", kind="показатель по годам", metric="средний срок рассмотрения (пункт 2.5)",
                        unit="календарных дней", clause="2.5", for_rows=[2],
                        sources=[SourceRef(row_id=2, metric="средний срок рассмотрения")]),
            RequestItem(num="3", kind="перечень или реестр",
                        metric="перечень отклонённых заявок с датой и основанием (пункт 5.1.4)", unit=None,
                        clause="5.1.4", for_rows=[1], sources=[SourceRef(row_id=1, metric="перечень отклонённых заявок")]),
        ],
        years=[2023, 2024, 2025],
    )


def test_request_sheet_shows_unit_with_metric(tmp_runs_dir):
    """Г1. Единица измерения — отдельное поле контракта; в бланке чиновник видит её
    рядом с показателем: «…, ед.», «…, календарных дней»."""
    path = build_excel(_map(), _request_v2(), _buckets(with_failed=False), _summary(), "run_unit")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, anchor="Показатель")
    col = _col(ws, hdr, "Показатель")
    cells = [ws.cell(row=r, column=col).value for r in range(hdr + 1, hdr + 3)]
    assert cells[0] == "число заявок (пункт 5.1.4), ед."
    assert cells[1] == "средний срок рассмотрения (пункт 2.5), календарных дней"


def test_request_sheet_puts_lists_in_separate_block_without_years(tmp_runs_dir):
    """Г2. Перечни и реестры — после показателей, под своей подписью, клетки годов
    прочерком: «перечень — это надо не в этой таблице», «должно быть отдельным реестром»."""
    path = build_excel(_map(), _request_v2(), _buckets(with_failed=False), _summary(), "run_lists")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, anchor="Показатель")
    rows = list(ws.iter_rows(min_row=hdr + 1, values_only=True))
    texts = [str(r[1]) if r[1] is not None else "" for r in rows]
    cap = next(i for i, t in enumerate(texts) if "Перечни и реестры" in t)
    assert cap == 2, "подпись блока идёт сразу после двух показателей"
    list_row = rows[cap + 1]
    assert list_row[0] == "3"
    assert list_row[1].startswith("перечень отклонённых заявок")
    assert list_row[4:7] == ("—", "—", "—"), "клеток годов у перечня нет — прочерки"
    assert rows[0][4:7] == (None, None, None), "у показателя клетки годов пустые для заполнения"


def test_request_sheet_no_list_block_when_no_lists(tmp_runs_dir):
    """Г2, граница: без перечней подписи блока нет."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _summary(), "run_nolists")
    ws = load_workbook(path)["Запрос показателей"]
    assert not any("Перечни и реестры" in str(c.value) for row in ws.iter_rows() for c in row if c.value)


def test_request_sheet_shows_note_under_metric(tmp_runs_dir):
    """Решение владельца 2026-09-04 (вопрос 4): две похожие позиции остаются, но каждая
    несёт пояснение, чем отличается — печатается второй строкой клетки «Показатель»."""
    req = DataRequest(items=[
        RequestItem(num="1", kind="показатель по годам", metric="количество отборов, проведённых (пункт 2.13)",
                    unit="ед.", clause="2.13", for_rows=[1], sources=[SourceRef(row_id=1, metric="отборы")],
                    note="только дошедшие до протокола; объявленные, но не состоявшиеся — в № 2"),
    ], years=[2023, 2024, 2025])
    path = build_excel(_map(), req, _buckets(with_failed=False), _summary(), "run_note")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, anchor="Показатель")
    cell = ws.cell(row=hdr + 1, column=_col(ws, hdr, "Показатель")).value
    assert cell == "количество отборов, проведённых (пункт 2.13), ед.\nПояснение: только дошедшие до протокола; объявленные, но не состоявшиеся — в № 2"
