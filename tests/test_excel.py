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
    JournalEntry,
    MapRow,
    Marks,
    RequestItem,
    SourceRef,
    Verdict,
)
from app.feedback import _col, _find_header_row
from app.nodes.excel import build_excel, compute_years
from app.contracts import ActPassport
from app.trace import run_dir
import pytest


def _minimal_passport_dict() -> dict:
    """Минимальный валидный паспорт (все объекты «нет» с обоснованием) — для заглушек."""
    no = {"state": "нет", "justification": "искал по всему тексту, не установлено"}
    return {
        "requisites": {"kind": "приказ", "number": "N 1", "date": "1 января 2024 г.",
                       "authority": "Департамент", "title": "Об утверждении порядка"},
        "status": {"status": "принят", "status_basis": "есть номер, дата принятия и подпись должностного лица",
                   "effective_date": "21.08.2025", "effective_date_clause": None, "deferred": [], "repealed_clauses": []},
        "addressee": {"scope": "внешним лицам", "categories": [{"name": "перевозчики", "clause": "п. 1"}], "distinctions": []},
        "nature": {"actions": [{"action": "утверждает новое", "target": "Порядок", "clauses": ["п. 1"]}],
                   "issued_under": [], "input_kind": "акт изменений не вносит"},
        "requirements": {**no, "groups": []},
        "procedure": {**no, "participants": None, "result": None, "has_document_check": None, "refusal_grounds": None, "clauses": []},
        "documents": {**no, "items": [], "transfer_clauses": []},
        "deadlines": {**no, "items": []},
        "references": {**no, "acts": [], "authorities": []},
        "allocation": {"competitive": {**no, "kind": None, "resource": None, "clauses": []}, "appointed": {**no, "items": []}},
        "control": {**no, "authority": None, "subject": None, "measures": [], "outcomes": [], "clauses": []},
    }


def _map() -> HypothesisMap:
    """Карта с двумя строками: одна с показателем «запрос органу» (уйдёт в
    запрос) и «открытый» рядом, вторая — целиком «запрос органу»."""
    return HypothesisMap(rows=[
        MapRow(
            row_id=1, indicators=[1, 8], hypothesis="гипотеза 1", grounds="основание 1",
            effects=[Effect(affected="бизнес", metric="издержки заявителя, руб.", how_to_calculate="сумма расходов", expected_trend="рост показателя относительно года до вступления акта подтвердит гипотезу")],
            data=[
                # намеренно вперемешку: открытый первым — рендер обязан
                # поднять «запрос органу» наверх сам (фикс N 2)
                DataItem(metric="доля отказов", source="открытый", where="Мосстат"),
                DataItem(metric="число заявок", source="запрос органу"),
            ],
            proof="дословная цитата 1", clause="5.1.4",
        ),
        MapRow(
            row_id=2, indicators=[3], hypothesis="гипотеза 2", grounds="основание 2",
            effects=[Effect(affected="бизнес", metric="срок рассмотрения, дней", how_to_calculate="среднее по выборке", expected_trend="рост показателя относительно года до вступления акта подтвердит гипотезу")],
            data=[DataItem(metric="средний срок рассмотрения", source="запрос органу")],
        ),
    ], merges=[])


def _request() -> DataRequest:
    """Запрос: показатель «1» с двумя разрезами (для строки 1), показатель «2» без
    разрезов (для строки 2). years намеренно 4 — Excel выводит ровно годы бланка."""
    return DataRequest(
        items=[
            RequestItem(
                num="1", kind="показатель по годам", metric="число заявок (пункт 5.1.4)", unit="ед.", clause="5.1.4",
                for_rows=[1], sources=[SourceRef(row_id=1, metric="число заявок")],
                breakdown=["юридические лица", "индивидуальные предприниматели"],
            ),
            RequestItem(
                num="2", kind="показатель по годам", metric="средний срок рассмотрения", unit="календарных дней",
                for_rows=[2], sources=[SourceRef(row_id=2, metric="средний срок рассмотрения")], breakdown=[],
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


def _passport() -> ActPassport:
    """Паспорт акта с реквизитами по образцу живого прогона (приказ ДСМИ) — для шапки листа."""
    d = _minimal_passport_dict()
    d["requisites"] = {"kind": "приказ", "number": "N 02-02-313/21", "date": "9 августа 2021 г.",
                       "authority": "Департамент средств массовой информации и рекламы города Москвы",
                       "title": "Об утверждении Порядка организации торгов"}
    return ActPassport(**d)


def test_opens_with_openpyxl_and_five_sheets(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_a")
    wb = load_workbook(path)
    assert wb.sheetnames == [
        "Как заполнять", "Карта гипотез", "Запрос показателей", "Вопросы органу", "Вердикты", "Ошибки прогона",
    ]


def test_saved_as_08_result_in_run_dir(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_b")
    assert path == run_dir("run_b") / "08_result.xlsx"
    assert path.exists()


def test_failed_sheet_present_even_when_bucket_empty(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_c")
    ws = load_workbook(path)["Ошибки прогона"]
    hdr = _find_header_row(ws, "Что случилось")
    assert [c.value for c in ws[hdr]] == ["Индикатор", "Что случилось", "Попыток"]
    assert ws.max_row == hdr + 1  # шапка + одна строка «ошибок нет»
    assert ws.cell(row=hdr + 1, column=1).value == "ошибок нет"


def test_failed_sheet_lists_failures(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_d")
    ws = load_workbook(path)["Ошибки прогона"]
    hdr = _find_header_row(ws, "Что случилось")
    row = hdr + 1
    assert ws.max_row == row
    assert "содержание понятных требований" in ws.cell(row=row, column=_col(ws, hdr, "Индикатор")).value
    assert ws.cell(row=row, column=_col(ws, hdr, "Что случилось")).value == "таймаут процесса после 900 с"
    assert ws.cell(row=row, column=_col(ws, hdr, "Попыток")).value == 3


def _rows_by_indicator(ws, hdr: int) -> dict[int, int]:
    """Номер строки листа по номеру индикатора («3. …» в первой колонке)."""
    out = {}
    for r in range(hdr + 1, ws.max_row + 1):
        label = ws.cell(row=r, column=_col(ws, hdr, "Индикатор")).value
        if label:
            out[int(label.split(".")[0])] = r
    return out


# ----------------------------------------------------------------------
# Пул 3 (письмо заказчика 2026-09-04, п. 4): «объединить вердикты по всем
# индикаторам на одном листе». Решение владельца: нового листа не заводить,
# третий лист называется «Вердикты» и показывает все 12, с отсылкой туда,
# где подробности (карта — для нарушенных и «нет данных», лист ошибок — для сбоев).
# ----------------------------------------------------------------------


def test_verdicts_sheet_lists_all_buckets_sorted(tmp_runs_dir):
    """На листе все вердикты четырёх куч по возрастанию номера индикатора, пять колонок."""
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_e")
    ws = load_workbook(path)["Вердикты"]
    hdr = _find_header_row(ws, "Вердикт")
    assert [c.value for c in ws[hdr]] == [
        "Индикатор", "Вердикт", "Обоснование", "На чём основано", "Где подробнее",
        "Согласны?", "Если нет — какой верный", "Замечание", "Где искать",   # 2.7: + «Где искать» (В)
    ]
    assert ws.max_row == hdr + 6                       # 1, 8 нарушен; 3 нет данных; 2, 5; 9 ошибка
    labels = [ws.cell(row=r, column=1).value for r in range(hdr + 1, ws.max_row + 1)]
    assert [int(l.split(".")[0]) for l in labels] == [1, 2, 3, 5, 8, 9]
    verdicts = [ws.cell(row=r, column=_col(ws, hdr, "Вердикт")).value for r in range(hdr + 1, ws.max_row + 1)]
    assert verdicts == ["нарушен", "соблюдён", "нет данных", "не применимо", "нарушен", "ошибка"]


def test_verdicts_sheet_points_to_map_rows(tmp_runs_dir):
    """Нарушен и «нет данных» со строкой карты — ссылка на номер строки карты."""
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_e2")
    ws = load_workbook(path)["Вердикты"]
    hdr = _find_header_row(ws, "Вердикт")
    rows = _rows_by_indicator(ws, hdr)
    where = _col(ws, hdr, "Где подробнее")
    assert ws.cell(row=rows[1], column=where).value == "Карта гипотез, стр. 1"
    assert ws.cell(row=rows[8], column=where).value == "Карта гипотез, стр. 1"
    assert ws.cell(row=rows[3], column=where).value == "Карта гипотез, стр. 2"
    assert ws.cell(row=rows[2], column=where).value == "—"
    assert ws.cell(row=rows[5], column=where).value == "—"


def test_verdicts_sheet_failed_points_to_errors_sheet(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_e3")
    ws = load_workbook(path)["Вердикты"]
    hdr = _find_header_row(ws, "Вердикт")
    rows = _rows_by_indicator(ws, hdr)
    assert ws.cell(row=rows[9], column=_col(ws, hdr, "Где подробнее")).value == "лист «Ошибки прогона»"
    assert ws.cell(row=rows[9], column=_col(ws, hdr, "Обоснование")).value == "таймаут процесса после 900 с"


def test_verdicts_sheet_unverified_points_to_same_row_as_map(tmp_runs_dir):
    """«Нет данных» без строки карты: номер дописанной строки карты и на листе вердиктов один."""
    buckets = _buckets(with_failed=False)
    buckets.no_data.append(Verdict(
        indicator=10, name="учёт практики применения", verdict="нет данных",
        rationale="статистики жалоб нет", missing_data="число жалоб за 3 года",
    ))
    path = build_excel(_map(), _request(), buckets, _passport(), "run_e4")
    wb = load_workbook(path)
    ws_map = wb["Карта гипотез"]
    map_hdr = _find_header_row(ws_map)
    last = ws_map.max_row
    assert "10. учёт практики применения" in ws_map.cell(row=last, column=_col(ws_map, map_hdr, "Индикаторы")).value
    row_id = ws_map.cell(row=last, column=_col(ws_map, map_hdr, "№")).value
    assert row_id == 3                                  # карта: строки 1, 2 → дописана 3

    ws = wb["Вердикты"]
    hdr = _find_header_row(ws, "Вердикт")
    rows = _rows_by_indicator(ws, hdr)
    assert ws.cell(row=rows[10], column=_col(ws, hdr, "Где подробнее")).value == (
        f"Карта гипотез, стр. {row_id} — нужна ручная проверка"
    )


def test_request_sheet_years_come_from_request(tmp_runs_dir):
    """Колонки годов = годы бланка. С v2.3 их пишет код узла запроса от даты вступления
    акта в силу (Г5), а Excel до 2026-09-25 считал три года от сегодняшней даты заново:
    у 1112-ПП (в силе с 21.08.2025) бланк [2025], а в Excel были колонки 2023–2025."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_f")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, "Показатель")
    header = [c.value for c in ws[hdr]]
    assert header[:2] == ["№", "Показатель"]
    # колонки годов ищем по виду заголовка, а не по позиции: между «Показателем»
    # и годами могут появиться колонки связи с картой
    years = [h for h in header if h and str(h).isdigit() and len(str(h)) == 4]
    assert years == ["2020", "2021", "2022", "2023"]


def test_request_sheet_single_year_for_recent_act(tmp_runs_dir):
    """Акт в силе с 2025 года — в Excel одна колонка 2025, без пустых 2023 и 2024."""
    req = _request().model_copy(update={"years": [2025]})
    path = build_excel(_map(), req, _buckets(with_failed=False), _passport(), "run_f1")
    ws = load_workbook(path)["Запрос показателей"]
    header = [c.value for c in ws[_find_header_row(ws, "Показатель")]]
    assert [h for h in header if h and str(h).isdigit()] == ["2025"]


def test_breakdown_rendered_as_subrows(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_g")
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
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_h")
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


def test_header_line_comes_from_passport_requisites(tmp_runs_dir):
    """Шапка — ОДНА осмысленная строка вид/номер/дата/орган из паспорта акта
    (раньше парсилась регулярками из markdown саммари; правка владельца 2026-08-24:
    мусорная шапка хуже отсутствующей)."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_i")
    wb = load_workbook(path)
    ws_map = wb["Карта гипотез"]

    header_row = _find_header_row(ws_map)
    assert header_row == 3  # реквизиты + подпись листа перед заголовками колонок

    header = ws_map.cell(row=1, column=1).value
    assert header == ("приказ N 02-02-313/21 от 9 августа 2021 г. "
                      "(Департамент средств массовой информации и рекламы города Москвы)")

    for name, anchor in (
        ("Карта гипотез", "Индикаторы"),
        ("Запрос показателей", "Показатель"),
        ("Вердикты", "Вердикт"),
        ("Ошибки прогона", "Что случилось"),
    ):
        ws = wb[name]
        hdr = _find_header_row(ws, anchor)
        assert ws.cell(row=hdr, column=1).font.bold is True
        assert ws.cell(row=hdr, column=1).fill.fgColor.rgb.endswith("1F4E78")
        assert ws.freeze_panes == ws.cell(row=hdr + 1, column=1).coordinate


def test_header_line_for_draft_act_has_no_number_and_date(tmp_runs_dir):
    """Проект акта: номера и даты нет — шапка «вид (проект) (орган)», без «None»."""
    d = _minimal_passport_dict()
    d["requisites"] = {**d["requisites"], "number": None, "date": None}
    d["status"] = {**d["status"], "status": "проект", "effective_date": None}
    path = build_excel(_map(), _request(), _buckets(with_failed=False), ActPassport(**d), "run_draft")
    header = load_workbook(path)["Карта гипотез"].cell(row=1, column=1).value
    assert header == "приказ (проект) (Департамент)"


def test_no_header_when_passport_missing(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_j")
    ws_map = load_workbook(path)["Карта гипотез"]
    assert _find_header_row(ws_map) == 2   # только подпись листа, строки реквизитов нет


def test_map_sheet_row_content(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_k")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    assert [c.value for c in ws[hdr]] == [
        "№", "Индикаторы", "Гипотеза", "Эффект", "Данные", "На чём основано",
        "Оценка (1–5)", "Замечание",
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
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_ind_names")
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
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_ind_width")
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
    """Блок эффекта подписан: кого задевает, показатель, как считать."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_effect")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    cell = ws.cell(row=hdr + 1, column=_col(ws, hdr, "Эффект")).value

    assert cell == (
        "Кого задевает: бизнес\n"
        "Показатель:\n"
        "издержки заявителя, руб.\n"
        "\n"
        "Как считать:\n"
        "сумма расходов\n"
        "\n"
        "Что подтвердит гипотезу:\n"
        "рост показателя относительно года до вступления акта подтвердит гипотезу"
    )


def test_effect_cell_lists_every_effect(tmp_runs_dir):
    """Контракт v2: у гипотезы несколько эффектов (бизнес и город от одного
    механизма) — в клетке оба блока, через пустую строку, ни один не потерян."""
    hyp_map = _map()
    hyp_map.rows[0].effects.append(
        Effect(affected="город", metric="трудозатраты комиссии, чел.-часов", how_to_calculate="заседания × состав", expected_trend="рост показателя относительно года до вступления акта подтвердит гипотезу")
    )
    path = build_excel(hyp_map, _request(), _buckets(with_failed=False), None, "run_effects2")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    cell = ws.cell(row=hdr + 1, column=_col(ws, hdr, "Эффект")).value
    assert "Кого задевает: бизнес" in cell and "Кого задевает: город" in cell
    assert "трудозатраты комиссии" in cell
    assert cell.count("Как считать:") == 2
    assert cell.count("Что подтвердит гипотезу:") == 2   # пул 3, п. 1б


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
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_data_group")
    cell = _data_cell(path)
    assert cell.index("Запрос в орган") < cell.index("Открытые источники")
    assert cell.index("число заявок") < cell.index("доля отказов")


def test_data_cell_open_sources_annotated(tmp_runs_dir):
    """У открытого блока есть пояснение, что у органа это не запрашивают."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_data_annot")
    cell = _data_cell(path)
    assert "Открытые источники (берём сами, у органа не запрашиваем):" in cell
    assert "Мосстат" in cell           # адрес открытого источника сохранён
    assert "(открытый" not in cell     # старая пометка в скобках убрана


def test_data_cell_request_nums_on_block_not_items(tmp_runs_dir):
    """Номер строки запроса стоит один раз в заголовке блока, а не у показателя."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_data_nums")
    cell = _data_cell(path)
    lines = cell.splitlines()

    head = [ln for ln in lines if ln.startswith("Запрос в орган")]
    assert head == ["Запрос в орган (лист «Запрос показателей», № 1):"]
    # ни одна строка-показатель номера не несёт
    assert [ln for ln in lines if ln.startswith("—") and "№" in ln] == []
    assert cell.count("№") == 1


def test_data_cell_omits_empty_group(tmp_runs_dir):
    """Строка без открытых показателей не печатает пустой заголовок группы."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_data_empty")
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
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_basis_hdr")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    headers = [c.value for c in ws[hdr]]
    assert "На чём основано" in headers
    assert "Пруф" not in headers
    assert "Пункт" not in headers


def test_basis_cell_has_norm_label(tmp_runs_dir):
    """Цитата подписана как норма акта, с номером пункта из строки карты."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_basis_norm")
    cell = _basis_cell(path)
    assert cell.startswith("Норма акта, п. 5.1.4:\n«дословная цитата 1»")


def test_basis_clause_not_overridden_by_verdict(tmp_runs_dir):
    """Пункт берётся из строки карты (5.1.4), а не из вердикта (9.9)."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_basis_clause")
    cell = _basis_cell(path)
    assert "п. 5.1.4" in cell
    assert "9.9" not in cell


def test_basis_cell_carries_web_sources(tmp_runs_dir):
    """Адреса из вердиктов индикаторов строки доезжают до документа, без дублей."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_basis_src")
    cell = _basis_cell(path)
    assert "Внешние источники:" in cell
    assert "— https://practice.example/1" in cell
    assert "— https://practice.example/2" in cell
    assert cell.count("https://practice.example/1") == 1   # индикаторы 1 и 8 дали дубль


def test_basis_cell_falls_back_to_verdict_proof(tmp_runs_dir):
    """Строка карты без цитаты берёт цитату вердикта своего индикатора."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_basis_fb")
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


def test_verdicts_sheet_basis_column(tmp_runs_dir):
    """Та же колонка «На чём основано», что и в карте, с теми же блоками."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_chk_basis")
    ws = load_workbook(path)["Вердикты"]
    hdr = _find_header_row(ws, "Вердикт")
    rows = _rows_by_indicator(ws, hdr)

    basis = ws.cell(row=rows[2], column=_col(ws, hdr, "На чём основано")).value
    assert basis.startswith("Норма акта, п. 2.1:\n«цитата»")
    assert "Внешние источники:" in basis
    assert "— https://docs.cntd.ru/document/3666532" in basis
    assert "— https://www.mos.ru/depr/" in basis

    # индикатор 5 «не применимо» — ни цитаты, ни адресов, но клетка не пустая
    assert ws.cell(row=rows[5], column=_col(ws, hdr, "На чём основано")).value == (
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
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_rownum")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)

    assert [c.value for c in ws[hdr]] == [
        "№", "Индикаторы", "Гипотеза", "Эффект", "Данные", "На чём основано",
        "Оценка (1–5)", "Замечание",
    ]
    num = _col(ws, hdr, "№")
    assert ws.cell(row=hdr + 1, column=num).value == 1
    assert ws.cell(row=hdr + 2, column=num).value == 2


def test_request_sheet_shows_source_rows(tmp_runs_dir):
    """Показатель, обслуживающий две строки карты, показывает обе с индикаторами."""
    request = DataRequest(
        items=[RequestItem(num="1", kind="показатель по годам", metric="число заявок", unit="ед.", clause="4",
                           for_rows=[1, 2], breakdown=[],
                           sources=[SourceRef(row_id=1, metric="число заявок"), SourceRef(row_id=2, metric="число заявок")])],
        years=[2020, 2021, 2022],
    )
    path = build_excel(_map(), request, _buckets(with_failed=False), None, "run_req_src")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, "Показатель")

    assert ws.cell(row=hdr + 1, column=_col(ws, hdr, "Откуда запрос")).value == (
        "стр. 1 — инд. 1, 8\nстр. 2 — инд. 3"
    )


def test_request_sheet_shows_clause(tmp_runs_dir):
    """Пункт акта из RequestItem.clause выводится своей колонкой."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_req_clause")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, "Показатель")
    assert ws.cell(row=hdr + 1, column=_col(ws, hdr, "Пункт акта")).value == "5.1.4"
    # у показателя «2» пункта нет — клетка пустая, а не «None»
    assert ws.cell(row=hdr + 4, column=_col(ws, hdr, "Пункт акта")).value in (None, "")


def test_request_breakdown_subrows_have_no_backref(tmp_runs_dir):
    """Подстроки разрезов ссылок не несут — они принадлежат родительской строке."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_req_sub")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, "Показатель")
    src = _col(ws, hdr, "Откуда запрос")
    assert ws.cell(row=hdr + 2, column=_col(ws, hdr, "№")).value == "1.1"
    assert ws.cell(row=hdr + 2, column=src).value in (None, "")


def test_backref_round_trip(tmp_runs_dir):
    """Из карты в запрос и обратно — оба номера ведут на существующие строки."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_roundtrip")
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
# знаке), поэтому смысл выносится первой строкой листа. С пула 3 (2026-09-04)
# третий лист — «Вердикты» со всеми индикаторами, подпись осталась.
# ----------------------------------------------------------------------

_SHEETS_AND_ANCHORS = (
    ("Карта гипотез", "Индикаторы"),
    ("Запрос показателей", "Показатель"),
    ("Вопросы органу", "Вопрос"),          # 2.7 (Ж2)
    ("Вердикты", "Вердикт"),
    ("Ошибки прогона", "Что случилось"),
)


def test_sheet_names_say_what_is_on_them(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_names")
    wb = load_workbook(path)
    assert wb.sheetnames == ["Как заполнять"] + [name for name, _ in _SHEETS_AND_ANCHORS]


def test_each_sheet_has_caption_above_header(tmp_runs_dir):
    """На каждом листе есть строка-подпись, шапка колонок сдвинута под неё."""
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_caption")
    wb = load_workbook(path)
    for name, anchor in _SHEETS_AND_ANCHORS:
        ws = wb[name]
        hdr = _find_header_row(ws, anchor)
        caption = ws.cell(row=hdr - 1, column=1).value
        assert caption and len(caption) > 30, f"нет подписи на листе «{name}»"
        assert ws.cell(row=hdr - 1, column=1).font.italic is True


def test_map_sheet_keeps_requisites_above_caption(tmp_runs_dir):
    """Реквизиты акта (из паспорта) остаются самой первой строкой, подпись — под ними."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_cap_req")
    ws = load_workbook(path)["Карта гипотез"]
    assert _find_header_row(ws) == 3            # реквизиты + подпись + шапка
    assert "N 02-02-313/21" in ws.cell(row=1, column=1).value
    assert ws.cell(row=2, column=1).value.startswith("Индикаторы, по которым найдено нарушение")


def test_caption_counts_come_from_data_not_hardcoded(tmp_runs_dir):
    """Числитель считается по факту прогона, знаменатель — из конфига."""
    from app.config import all_indicators

    total = len(all_indicators())
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_cap_cnt")
    wb = load_workbook(path)

    # в карте две строки: индикаторы 1, 8 и 3 — три уникальных
    assert f"3 из {total}" in wb["Карта гипотез"].cell(row=2, column=1).value
    # вердиктов во всех кучах шесть (1, 8, 3, 2, 5, 9)
    assert f"6 из {total}" in wb["Вердикты"].cell(row=1, column=1).value


def test_caption_does_not_break_header_styling(tmp_runs_dir):
    """Заливка шапки и freeze panes встали на сдвинутую строку."""
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_cap_style")
    wb = load_workbook(path)
    for name, anchor in _SHEETS_AND_ANCHORS:
        ws = wb[name]
        hdr = _find_header_row(ws, anchor)
        assert ws.cell(row=hdr, column=1).font.bold is True
        assert ws.cell(row=hdr, column=1).fill.fgColor.rgb.endswith("1F4E78")
        assert ws.freeze_panes == ws.cell(row=hdr + 1, column=1).coordinate
        # подпись заливки шапки не получила
        assert ws.cell(row=hdr - 1, column=1).fill.fgColor.rgb != "FF1F4E78"


# ----------------------------------------------------------------------
# Пул 3 (письмо заказчика 2026-09-04, п. 6.1): «включить оранжевые столбцы
# (для заполнения экспертами) сразу в выгружаемый файл». Решение владельца:
# всегда, одна книга. Образец — файлы экспертов ОС: заливка FFF2CC, на карте
# и запросе «Оценка (1–5)» и «Замечание», на вердиктах «Согласны?», «Если нет —
# какой верный», «Замечание»; на техническом листе ошибок колонок нет.
# ----------------------------------------------------------------------

_EXPERT_COLUMNS = {
    "Карта гипотез": ("Индикаторы", ["Оценка (1–5)", "Замечание"]),
    "Запрос показателей": ("Показатель", ["Оценка (1–5)", "Замечание"]),
    "Вопросы органу": ("Вопрос", ["Оценка (1–5)", "Замечание"]),                       # 2.7 (Ж2)
    "Вердикты": ("Вердикт", ["Согласны?", "Если нет — какой верный", "Замечание", "Где искать"]),  # 2.7 (В)
}


def test_expert_columns_present_and_orange(tmp_runs_dir):
    """На трёх листах справа — колонки эксперта с заливкой FFF2CC во всём теле; на листе ошибок их нет."""
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_expert")
    wb = load_workbook(path)
    for sheet, (anchor, names) in _EXPERT_COLUMNS.items():
        ws = wb[sheet]
        hdr = _find_header_row(ws, anchor)
        header = [c.value for c in ws[hdr]]
        assert header[-len(names):] == names, sheet
        for name in names:
            col = _col(ws, hdr, name)
            assert ws.cell(row=hdr, column=col).fill.fgColor.rgb.endswith("1F4E78"), (sheet, name)  # шапка общая
            for r in range(hdr + 1, ws.max_row + 1):
                cell = ws.cell(row=r, column=col)
                assert cell.fill.fgColor.rgb.endswith("FFF2CC"), (sheet, name, r)
                assert cell.border.left.style == "thin", (sheet, name, r)
    ws_err = wb["Ошибки прогона"]
    hdr = _find_header_row(ws_err, "Что случилось")
    assert [c.value for c in ws_err[hdr]] == ["Индикатор", "Что случилось", "Попыток"]


def test_howto_sheet_is_first_and_filled_by_service(tmp_runs_dir):
    """Лист-инструкция для эксперта: акт и дата — от сервиса, ФИО и дата отзыва — оранжевые, правила называют реальные листы."""
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_howto")
    wb = load_workbook(path)
    ws = wb["Как заполнять"]
    assert wb.sheetnames[0] == "Как заполнять"
    assert ws.cell(row=1, column=1).value == "Обратная связь по разбору нормативного акта"
    rows = {ws.cell(row=r, column=1).value: r for r in range(1, ws.max_row + 1)}
    assert "N 02-02-313/21" in ws.cell(row=rows["Акт"], column=2).value
    assert ws.cell(row=rows["Разобран"], column=2).value            # дата сборки, непустая
    for label in ("ФИО", "Дата отзыва"):
        cell = ws.cell(row=rows[label], column=2)
        assert cell.value is None and cell.fill.fgColor.rgb.endswith("FFF2CC"), label
    rules = "\n".join(str(ws.cell(row=r, column=2).value) for r in range(rows["Что нужно сделать"] + 1, ws.max_row + 1))
    for name in ("Карта гипотез", "Запрос показателей", "Вердикты"):
        assert f"«{name}»" in rules, name
    assert "Проверенные индикаторы" not in rules
    assert ws.max_row - rows["Что нужно сделать"] == 7   # 2.7: + правило про «Где искать»


def test_howto_sheet_without_passport_has_dash_for_act(tmp_runs_dir):
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_howto_np")
    ws = load_workbook(path)["Как заполнять"]
    rows = {ws.cell(row=r, column=1).value: r for r in range(1, ws.max_row + 1)}
    assert ws.cell(row=rows["Акт"], column=2).value == "—"


def test_expert_columns_have_dropdown_lists(tmp_runs_dir):
    """Оценка — список 1…5 на карте и запросе; «Согласны?» — да/нет на вердиктах (как в шаблоне экспертов ОС)."""
    path = build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_dv")
    wb = load_workbook(path)
    for sheet, anchor, name, formula in (
        ("Карта гипотез", "Индикаторы", "Оценка (1–5)", '"1,2,3,4,5"'),
        ("Запрос показателей", "Показатель", "Оценка (1–5)", '"1,2,3,4,5"'),
        ("Вердикты", "Вердикт", "Согласны?", '"да,нет"'),
    ):
        ws = wb[sheet]
        hdr = _find_header_row(ws, anchor)
        col_letter = ws.cell(row=hdr, column=_col(ws, hdr, name)).column_letter
        dvs = [dv for dv in ws.data_validations.dataValidation if dv.formula1 == formula]
        assert dvs, (sheet, name)
        covered = str(dvs[0].sqref)
        assert covered == f"{col_letter}{hdr + 1}:{col_letter}{ws.max_row}", (sheet, covered)


def test_expert_columns_cover_breakdown_rows(tmp_runs_dir):
    """Подстроки разрезов «1.1», «1.2» листа запроса тоже получают оранжевые клетки."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_expert_bd")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, "Показатель")
    col = _col(ws, hdr, "Оценка (1–5)")
    sub_rows = [r for r in range(hdr + 1, ws.max_row + 1) if str(ws.cell(row=r, column=1).value).startswith("1.")]
    assert sub_rows, "в фикстуре есть разрезы 1.1 и 1.2"
    for r in sub_rows:
        assert ws.cell(row=r, column=col).fill.fgColor.rgb.endswith("FFF2CC")


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
    path = build_excel(_map(), _request(), _buckets_with_unverified(), _passport(), "run_unverified")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    rows = list(ws.iter_rows(min_row=hdr + 1, values_only=True))
    assert len(rows) == 3, "две строки карты + одна строка незавершённой проверки"

    num, ind, hyp, effect, data, basis = rows[2][:6]
    assert num == 3, "нумерация продолжается за строками карты"
    assert ind == "4. отсутствие дублирования требований"
    assert hyp.startswith("Нужна ручная проверка")
    assert "текст статей Закона города Москвы от 13.11.2024 № 27" in hyp, "что не проверено — из missing_data"
    assert "не открылся" not in hyp, "2.5: весь ход проверки агента в клетку больше не идёт"
    assert "запрос органу не идёт" in data
    assert "цитата вердикта 4" in basis and "2.6" in basis


def test_map_caption_counts_unverified_indicators(tmp_runs_dir):
    """Б1. Подпись карты обещает «нарушено или не хватило данных — N из 12»:
    незавершённая проверка входит в N."""
    path = build_excel(_map(), _request(), _buckets_with_unverified(), _passport(), "run_unverified_cap")
    ws = load_workbook(path)["Карта гипотез"]
    hdr = _find_header_row(ws)
    caption = str(ws.cell(row=hdr - 1, column=1).value)
    assert "4 из 12" in caption, caption   # индикаторы 1, 8, 3 из карты + 4 незавершённый


def test_map_no_unverified_row_when_no_data_has_hypotheses(tmp_runs_dir):
    """Б1, граница: «нет данных» по индикатору 3 уже представлен строкой карты 2 —
    второй строки для него не появляется."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_no_dup")
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
    path = build_excel(_map(), _request_v2(), _buckets(with_failed=False), _passport(), "run_unit")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, anchor="Показатель")
    col = _col(ws, hdr, "Показатель")
    cells = [ws.cell(row=r, column=col).value for r in range(hdr + 1, hdr + 3)]
    assert cells[0] == "число заявок (пункт 5.1.4), ед."
    assert cells[1] == "средний срок рассмотрения (пункт 2.5), календарных дней"


def test_request_sheet_puts_lists_in_separate_block_without_years(tmp_runs_dir):
    """Г2. Перечни и реестры — после показателей, под своей подписью, клетки годов
    прочерком: «перечень — это надо не в этой таблице», «должно быть отдельным реестром»."""
    path = build_excel(_map(), _request_v2(), _buckets(with_failed=False), _passport(), "run_lists")
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
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_nolists")
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
    path = build_excel(_map(), req, _buckets(with_failed=False), _passport(), "run_note")
    ws = load_workbook(path)["Запрос показателей"]
    hdr = _find_header_row(ws, anchor="Показатель")
    cell = ws.cell(row=hdr + 1, column=_col(ws, hdr, "Показатель")).value
    assert cell == "количество отборов, проведённых (пункт 2.13), ед.\nПояснение: только дошедшие до протокола; объявленные, но не состоявшиеся — в № 2"


def test_basis_cell_carries_grounds_from_verdict(tmp_runs_dir):
    """Контракт v2: основание гипотезы (дословно из карточки вердикта) — в клетке
    «На чём основано», последним блоком, после нормы и источников."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), None, "run_grounds")
    cell = _basis_cell(path)
    assert cell.startswith("Норма акта, п. 5.1.4:")
    assert cell.endswith("Основание из вердикта:\nоснование 1")


# ---- Excel для версий диалогового режима (план итерации 3, шаг 1.5) ----
# Без новых параметров книга прежняя; с ними — номер версии, журнал изменений,
# колонка «Изменения» (правлено человеком / устарело) и закреплённые номера
# строк «проверка не завершена».


def _journal() -> list[JournalEntry]:
    return [
        JournalEntry(version=1, at="2026-09-26T10:00:00", kind="решение эксперта", target="Вердикты, инд. 3",
                     action="вердикт изменён", before="соблюдён", after="нарушен",
                     reason="эксперт: пятнадцать форм избыточны"),
        JournalEntry(version=2, at="2026-09-26T10:05:00", kind="правка", target="Карта гипотез, стр. 2",
                     action="переформулирована гипотеза", before="гипотеза 2", after="гипотеза 2, короче",
                     reason="убрать воду", cost_usd=0.12),
    ]


def test_default_book_unchanged_by_version_features(tmp_runs_dir):
    """Без параметров версий: шесть листов (2.7: + «Вопросы органу»), колонки «Изменения» нет, строки «Версия файла» нет."""
    wb = load_workbook(build_excel(_map(), _request(), _buckets(with_failed=True), _passport(), "run_v0"))
    assert wb.sheetnames == ["Как заполнять", "Карта гипотез", "Запрос показателей", "Вопросы органу", "Вердикты",
                             "Ошибки прогона"]
    for sheet, anchor in (("Карта гипотез", "Индикаторы"), ("Запрос показателей", "Показатель"), ("Вердикты", "Вердикт")):
        ws = wb[sheet]
        assert "Изменения" not in [c.value for c in ws[_find_header_row(ws, anchor)]]
    assert "Версия файла" not in [c.value for c in wb["Как заполнять"]["A"]]


def test_version_row_and_original_analysis_date(tmp_runs_dir):
    """Номер версии и дата исходного разбора (а не момент пересборки) — на листе «Как заполнять»."""
    import datetime
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_v2",
                       version=2, analyzed_at=datetime.datetime(2026, 9, 7, 15, 17))
    ws = load_workbook(path)["Как заполнять"]
    rows = {r[0].value: r[1].value for r in ws.iter_rows(max_col=2) if r[0].value}
    assert rows["Разобран"] == "7 сентября 2026, 15:17"
    assert rows["Версия файла"].startswith("2")


def test_journal_sheet_lists_changes(tmp_runs_dir):
    """Лист «Журнал изменений» — последний, по строке на изменение."""
    path = build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_v3",
                       version=2, journal=_journal())
    wb = load_workbook(path)
    assert wb.sheetnames[-1] == "Журнал изменений"
    ws = wb["Журнал изменений"]
    hdr = _find_header_row(ws, "Что сделано")
    body = [r for r in ws.iter_rows(min_row=hdr + 1, values_only=True) if any(r)]
    assert len(body) == 2
    assert body[0][_col(ws, hdr, "Где") - 1] == "Вердикты, инд. 3"
    assert body[1][_col(ws, hdr, "Почему") - 1] == "убрать воду"


def test_marks_column_on_three_sheets(tmp_runs_dir):
    """Колонка «Изменения» — перед оранжевыми колонками, пометка у нужной строки."""
    marks = Marks(edited_verdicts=[3], stale_rows={2: "вердикт инд. 3 изменён — пересчитать"},
                  stale_items={"1": "строка карты 1 изменена"})
    wb = load_workbook(build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_v4",
                                   version=1, marks=marks))
    ws = wb["Вердикты"]; hdr = _find_header_row(ws, "Вердикт")
    names = [c.value for c in ws[hdr]]
    assert names.index("Изменения") < names.index("Согласны?")
    assert ws.cell(row=_rows_by_indicator(ws, hdr)[3], column=_col(ws, hdr, "Изменения")).value == "правлено человеком"
    ws = wb["Карта гипотез"]; hdr = _find_header_row(ws, "Индикаторы")
    by_id = {ws.cell(row=r, column=1).value: r for r in range(hdr + 1, ws.max_row + 1)}
    assert "устарело" in ws.cell(row=by_id[2], column=_col(ws, hdr, "Изменения")).value
    assert not ws.cell(row=by_id[1], column=_col(ws, hdr, "Изменения")).value
    ws = wb["Запрос показателей"]; hdr = _find_header_row(ws, "Показатель")
    by_num = {ws.cell(row=r, column=1).value: r for r in range(hdr + 1, ws.max_row + 1)}
    assert "устарело" in ws.cell(row=by_num["1"], column=_col(ws, hdr, "Изменения")).value
    assert not ws.cell(row=by_num["1.1"], column=_col(ws, hdr, "Изменения")).value   # разрез — без пометки


def test_fixed_unverified_ids_on_map_and_verdicts(tmp_runs_dir):
    """Закреплённый номер строки «проверка не завершена» — один и тот же на карте и в «Где подробнее»."""
    wb = load_workbook(build_excel(_map(), _request(), _buckets_with_unverified(), _passport(), "run_v5",
                                   unverified_ids={4: 9}))
    ws = wb["Карта гипотез"]; hdr = _find_header_row(ws, "Индикаторы")
    assert 9 in [ws.cell(row=r, column=1).value for r in range(hdr + 1, ws.max_row + 1)]
    ws = wb["Вердикты"]; hdr = _find_header_row(ws, "Вердикт")
    where = ws.cell(row=_rows_by_indicator(ws, hdr)[4], column=_col(ws, hdr, "Где подробнее")).value
    assert where == "Карта гипотез, стр. 9 — нужна ручная проверка"



# ---- 2.5: выжимка для эксперта — в Excel пункты, полный текст остаётся в JSON ----

def _map_with_briefs() -> HypothesisMap:
    m = _map()
    row = m.rows[0]
    effects = [e.model_copy(update={"how_to_calculate_brief": ["считаем за год"],
                                    "expected_trend_brief": ["рост подтвердит"]}) for e in row.effects]
    first = row.model_copy(update={"hypothesis_brief": ["что не так (п. 5.1.4)", "кого задевает"],
                                   "grounds_brief": ["вывод вердикта (п. 5.1.4)"], "effects": effects})
    return m.model_copy(update={"rows": [first] + m.rows[1:]})


def test_map_shows_expert_briefs_as_points(tmp_runs_dir):
    ws = load_workbook(build_excel(_map_with_briefs(), _request(), _buckets(with_failed=False), _passport(),
                                   "run_brief"))["Карта гипотез"]
    hdr = _find_header_row(ws)
    cell = lambda r, name: ws.cell(row=hdr + r, column=_col(ws, hdr, name)).value   # noqa: E731
    assert cell(1, "Гипотеза") == "• что не так (п. 5.1.4)\n• кого задевает"
    effect = cell(1, "Эффект")
    assert "Как считать:\n• считаем за год" in effect and "Что подтвердит гипотезу:\n• рост подтвердит" in effect
    assert "сумма расходов" not in effect
    basis = cell(1, "На чём основано")
    assert "Основание из вердикта:\n• вывод вердикта (п. 5.1.4)" in basis and "основание 1" not in basis
    assert "5.1.4" in basis, "цитата и пункт акта — без изменений"
    assert cell(2, "Гипотеза") == "гипотеза 2", "строка без выжимки — полный текст, как до 2.5"


def test_verdicts_sheet_shows_brief_not_full_rationale(tmp_runs_dir):
    b = _buckets(with_failed=False)
    first = b.violated[0].model_copy(update={"rationale_brief": ["пункт 1 (п. 9.9)", "пункт 2"]})
    b = b.model_copy(update={"violated": [first] + b.violated[1:]})
    ws = load_workbook(build_excel(_map(), _request(), b, _passport(), "run_vb"))["Вердикты"]
    hdr = _find_header_row(ws, "Вердикт")
    rows = _rows_by_indicator(ws, hdr)
    assert ws.cell(row=rows[1], column=_col(ws, hdr, "Обоснование")).value == "• пункт 1 (п. 9.9)\n• пункт 2"
    assert ws.cell(row=rows[8], column=_col(ws, hdr, "Обоснование")).value == "пакет избыточен"


def test_unverified_row_shows_looked_and_not_found(tmp_runs_dir):
    b = _buckets_with_unverified()
    brief = ["Смотрел: текст Закона города Москвы от 13.11.2024 № 27 — КонсультантПлюс, ГАРАНТ.",
             "Не нашёл: текст статей закона — не открылся, проверьте вручную."]
    b = b.model_copy(update={"no_data": [v.model_copy(update={"rationale_brief": brief}) if v.indicator == 4 else v
                                         for v in b.no_data]})
    ws = load_workbook(build_excel(_map(), _request(), b, _passport(), "run_unv_brief"))["Карта гипотез"]
    hdr = _find_header_row(ws)
    rows = list(ws.iter_rows(min_row=hdr + 1, values_only=True))
    hyp = rows[2][2]
    assert hyp == "Нужна ручная проверка.\n\n• " + "\n• ".join(brief)
    assert "сопоставить требования" not in hyp


# ---- 2.7 (итерация 4): Ж2, Ж3, В ----


def _map_iter4() -> HypothesisMap:
    """Строка 1: цифра органу, вопрос органу, открытый источник и цифра, которую нигде не ведут."""
    return HypothesisMap(rows=[MapRow(
        row_id=1, indicators=[11], hypothesis="гипотеза про сроки ГЭЭ", grounds="основание",
        effects=[Effect(affected="бизнес и город", metric="дни ожидания", how_to_calculate="разность сроков",
                        expected_trend="рост доли продлений относительно 2023 года подтвердит гипотезу")],
        data=[
            DataItem(metric="число продлений срока ГЭЭ", source="запрос органу"),
            DataItem(metric="Назовите три наиболее частых основания для продления срока ГЭЭ", source="вопрос органу"),
            DataItem(metric="средняя зарплата эксперта", source="открытый", where="Росстат"),
            DataItem(metric="число погибших по видам нарушений", source="нигде не ведётся"),
        ],
        proof="цитата", clause="2.7.1.2",
    )])


def _request_iter4() -> DataRequest:
    return DataRequest(items=[
        RequestItem(num="1", kind="показатель по годам", metric="число продлений срока ГЭЭ (п. 2.7.1.2)", unit="ед.",
                    clause="2.7.1.2", for_rows=[1], sources=[SourceRef(row_id=1, metric="число продлений срока ГЭЭ")]),
        RequestItem(num="2", kind="открытый вопрос",
                    metric="Назовите три наиболее частых основания для продления срока ГЭЭ", clause="2.7.1.2",
                    for_rows=[1], sources=[SourceRef(row_id=1, metric="Назовите три наиболее частых основания для продления срока ГЭЭ")]),
    ], years=[2023, 2024, 2025])


def _sheet_rows(ws) -> list[list]:
    return [[c.value for c in row] for row in ws.iter_rows()]


def test_questions_sheet_after_request_and_not_in_request(tmp_runs_dir):
    """Ж2: вопросы органу — отдельный лист после «Запроса показателей»; в самом запросе их нет."""
    wb = load_workbook(build_excel(_map_iter4(), _request_iter4(), _buckets(with_failed=False), _passport(), "run_q"))
    names = wb.sheetnames
    assert names.index("Вопросы органу") == names.index("Запрос показателей") + 1
    req_text = str(_sheet_rows(wb["Запрос показателей"]))
    assert "Назовите три наиболее частых" not in req_text and "число продлений срока ГЭЭ" in req_text
    ws = wb["Вопросы органу"]
    rows = _sheet_rows(ws)
    header = next(r for r in rows if r and r[0] == "№")
    assert header[:4] == ["№", "Вопрос", "Пункт акта", "Строки карты"]
    assert "Оценка (1–5)" in header and "Замечание" in header
    q = next(r for r in rows if r and r[0] == "2")
    assert q[1] == "Назовите три наиболее частых основания для продления срока ГЭЭ" and q[2] == "2.7.1.2"
    assert "стр. 1" in q[3]
    assert "Вопросы органу словами" in str(rows[0])


def test_questions_sheet_says_none_when_empty(tmp_runs_dir):
    wb = load_workbook(build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_q0"))
    assert "вопросов нет" in str(_sheet_rows(wb["Вопросы органу"]))


def test_map_data_cell_groups_question_and_not_tracked(tmp_runs_dir):
    """Ж2, Ж3: в графе «Данные» — запрос в орган, вопрос органу, открытые, нигде не ведётся."""
    wb = load_workbook(build_excel(_map_iter4(), _request_iter4(), _buckets(with_failed=False), _passport(), "run_d"))
    ws = wb["Карта гипотез"]
    cell = next(str(r[4]) for r in _sheet_rows(ws) if r and r[0] == 1)
    assert "Запрос в орган (лист «Запрос показателей», № 1):" in cell
    assert "Вопрос органу (лист «Вопросы органу», № 2):" in cell
    assert "Нигде не ведётся (отметил эксперт):\n— число погибших по видам нарушений" in cell
    opened = cell.split("Открытые источники")[1].split("Нигде не ведётся")[0]
    assert "число погибших" not in opened and "Назовите" not in opened


def test_verdicts_no_data_highlighted_and_where_to_search_column(tmp_runs_dir):
    """В: строки «нет данных» подсвечены, у эксперта графа «Где искать»."""
    wb = load_workbook(build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_w"))
    ws = wb["Вердикты"]
    rows = list(ws.iter_rows())
    header = next(r for r in rows if r[0].value == "Индикатор")
    names = [c.value for c in header]
    assert names.index("Где искать") == names.index("Замечание") + 1
    no_data = next(r for r in rows if r[1].value == "нет данных")
    other = next(r for r in rows if r[1].value == "нарушен")
    assert no_data[0].fill.fgColor.rgb != other[0].fill.fgColor.rgb
    assert "Где искать" in str(ws.cell(1, 1).value)



def test_request_sheet_mandatory_block_and_origin(tmp_runs_dir):
    """АМ: сначала «Обязательные показатели (тип акта: …)», затем «Показатели из гипотез»; в «Откуда» — код формы."""
    req = DataRequest(items=[
        RequestItem(num="1", kind="показатель по годам", metric="Общее количество заявок, поданных на получение поддержки",
                    unit="ед.", mandatory=["ГП-1"]),
        RequestItem(num="2", kind="показатель по годам", metric="число заявок (пункт 5.1.4)", unit="ед.", clause="5.1.4",
                    for_rows=[1], sources=[SourceRef(row_id=1, metric="число заявок")]),
    ], years=[2023, 2024, 2025])
    wb = load_workbook(build_excel(_map(), req, _buckets(with_failed=False), _passport(), "run_m",
                                   act_types=["господдержка"]))
    rows = [[c.value for c in r] for r in wb["Запрос показателей"].iter_rows()]
    labels = [r[1] for r in rows]
    i_m = labels.index("Обязательные показатели (тип акта: господдержка)")
    i_h = labels.index("Показатели из гипотез")
    assert i_m < labels.index("Общее количество заявок, поданных на получение поддержки, ед.") < i_h
    assert next(r for r in rows if r[0] == "1")[2] == "обязательный (ГП-1)"
    howto = wb["Как заполнять"]
    tip = next(howto.cell(r, 2).value for r in range(1, howto.max_row + 1) if howto.cell(r, 1).value == "Тип акта")
    assert tip == "господдержка"



def test_developers_in_map_header_and_request_caption(tmp_runs_dir):
    """А: разработчик — в шапке карты; лист запроса — «Заполняет: …» вместо общей фразы."""
    from app.contracts import Developer
    p = _passport()
    p = p.model_copy(update={"requisites": p.requisites.model_copy(update={"developers": [
        Developer(name="Департамент предпринимательства и инновационного развития города Москвы",
                  scope="приложение 1", clauses="п. 1.5 приложения 1", basis="приём заявок поручен департаменту"),
    ]})})
    wb = load_workbook(build_excel(_map(), _request(), _buckets(with_failed=False), p, "run_dev"))
    head = str(wb["Карта гипотез"].cell(1, 1).value)
    assert "Разработчик: Департамент предпринимательства и инновационного развития города Москвы (приложение 1;" in head
    cap = str(wb["Запрос показателей"].cell(1, 1).value)
    assert cap.startswith("Заполняет: Департамент предпринимательства")


def test_request_caption_without_developers_unchanged(tmp_runs_dir):
    wb = load_workbook(build_excel(_map(), _request(), _buckets(with_failed=False), _passport(), "run_nodev"))
    assert str(wb["Запрос показателей"].cell(1, 1).value).startswith("Данные, которых нет в открытом доступе. Заполняет орган-разработчик")
