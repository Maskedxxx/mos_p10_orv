"""
Узел [7] пайплайна П-10 — детерминированная сборка итогового .xlsx (app/nodes/excel.py).

Модель не используется — чистый код, как и в фазе 2 (см. build_excel.py той
версии, откуда переиспользованы стили оформления и compute_years). Собирает
книгу с четырьмя листами:

    Карта гипотез          — строки карты: индикаторы, гипотеза, эффект, данные, пруф, пункт
    Запрос показателей     — номер, показатель, три колонки годов (пустые), разрезы подстроками
    Проверенные индикаторы — куча buckets.checked: индикатор, вердикт, обоснование, пруф, пункт
    Ошибки прогона         — куча buckets.failed: индикатор, что случилось, попыток

Оформление — единый деловой стиль фазы 2 (тёмно-синяя шапка 1F4E78, белый
жирный шрифт, перенос текста, тонкие рамки, freeze panes) — стилевые константы
и подход к _style_header_row/_format_*_sheet перенесены как есть, отработаны
и приняты владельцем по прошлому листу. Отличие: _style_header_row теперь
принимает номер строки шапки, а не жёстко берёт первую строку — на листе
«Карта гипотез» перед шапкой может стоять одна строка с реквизитами акта.

ОБРАТНЫЕ ССЫЛКИ между листами «Карта гипотез» и «Запрос показателей» строятся
на уровне СТРОКИ карты (row_id), не отдельного показателя: контракт
RequestItem.for_rows ссылается на MapRow.row_id, а не на конкретный DataItem
внутри строки (в отличие от фазы 2, где обратные ссылки строил сам build_excel
по показателям). Раз узел [6] уже решает, как сгруппировать показатели в
запрос, здесь просто читаем его решение и раскладываем по строкам —
детализировать точнее нечем (см. _row_to_request_nums).

Глубина запроса — REQUEST_YEARS_DEPTH (3) лет, посчитанных детерминированно
через compute_years, а НЕ взятых из DataRequest.years — так шапка листа
не зависит от того, что вернула модель узла [6] в этом поле.

Шапка листа «Карта гипотез» — ОДНА строка с реквизитами акта (вид, номер,
дата, орган), собранная из секции "1. Реквизиты" саммари (см. _extract_requisites).
Заголовок секции промпт саммари фиксирует, а вот формулировку подписи внутри
каждого поля — нет ("markdown, свободной структурой"), поэтому подписи ищутся
по ключевому слову, а не по точному тексту. Если распознать все четыре
реквизита не удалось — шапки на листе НЕ будет вовсе (правка владельца
2026-08-24: мусорная шапка из первых попавшихся строк саммари хуже, чем её
отсутствие), лист начинается сразу с заголовков колонок.

Лист «Ошибки прогона» создаётся ВСЕГДА, даже если buckets.failed пуст —
тогда с одной строкой «ошибок нет»: аналитик должен видеть, что прогон
отработал целиком, а не гадать, забыли лист или ошибок правда не было.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.worksheet import Worksheet

from app.config import REQUEST_YEARS_DEPTH
from app.contracts import Buckets, DataItem, DataRequest, Effect, HypothesisMap, Verdict
from app.logger import get_run_logger
from app.trace import run_dir

# --- единые стили оформления (перенесены из build_excel.py фазы 2 как есть) ---
_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")          # тёмно-синяя шапка
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)        # белый жирный
_BODY_FONT = Font(size=11)
_WRAP_TOP = Alignment(wrap_text=True, vertical="top")          # перенос, верх
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_CENTER_TOP = Alignment(horizontal="center", vertical="top")
_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

_SHEET_MAP = "Карта гипотез"
_SHEET_REQUEST = "Запрос показателей"
_SHEET_CHECKED = "Проверенные индикаторы"
_SHEET_FAILED = "Ошибки прогона"


def compute_years(num_years: int = 4, end_year: int | None = None) -> list[int]:
    """
    Диапазон из num_years лет, заканчивающийся на end_year (перенесено из фазы 2,
    формула диапазона не менялась).

    ПРАВКА владельца 2026-08-24: если end_year не задан явно, по умолчанию
    берётся ПРОШЛЫЙ год (today.year - 1), а не текущий. Текущий год ещё не
    завершился — просить у органа данные за неполный год значит получить
    либо дыру в клетке, либо восемь месяцев вместо двенадцати, оба исхода
    для сравнения не годятся. Явно переданный end_year уважается как есть,
    поправка касается только автоопределения.
    """
    if num_years < 1:
        raise ValueError("num_years должен быть >= 1")
    if end_year is None:
        end_year = datetime.date.today().year - 1
    return list(range(end_year - num_years + 1, end_year + 1))


def _style_header_row(ws: Worksheet, row: int) -> None:
    """
    Оформить строку шапки и закрепить прокрутку под ней (образец фазы 2,
    адаптирован под номер строки — на листе «Карта гипотез» перед шапкой
    может стоять строка с реквизитами акта).
    """
    for c in ws[row]:
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
        c.alignment = _CENTER
        c.border = _BORDER
    ws.row_dimensions[row].height = 30
    ws.freeze_panes = ws.cell(row=row + 1, column=1).coordinate


# Секция реквизитов в саммари — по промпту prompts/01_summary.md ("1. Реквизиты:
# вид акта, номер, дата, принявший орган, полное название"), заголовок раздела
# фиксирован промптом. Формулировка ПОДПИСЕЙ полей внутри — нет (промпт прямо
# требует "markdown, свободной структурой"), поэтому подписи матчим по ключевому
# слову, а не по точному тексту.
# Заголовок секции реквизитов. Промпт саммари задаёт смысл раздела, но не
# точную формулировку заголовка — на живом прогоне модель написала
# "## 1. Реквизиты акта" вместо "## 1. Реквизиты", и строгий якорь $ секцию
# не нашёл, из-за чего лист карты остался без шапки. Поэтому хвост после
# слова "Реквизиты" допускается, а номер пункта необязателен.
_REQUISITES_HEADING_RE = re.compile(r"^#{1,3}\s*(?:\d+\.?\s*)?Реквизиты\b.*$", re.MULTILINE | re.IGNORECASE)
_NEXT_HEADING_RE = re.compile(r"^##\s", re.MULTILINE)
_BULLET_FIELD_RE = re.compile(r"^-\s*\*\*(.+?):\*\*\s*(.+)$")

# Ключевые слова в подписи поля -> ключ в словаре реквизитов. Порядок важен
# только для читаемости, не для матчинга (проверка "подстрока в подписи").
_REQUISITE_KEYWORDS = {"вид": "вид", "номер": "номер", "дата": "дата", "орган": "орган"}


def _requisites_section(summary_md: str) -> str:
    """
    Вырезать текст секции "## 1. Реквизиты" — от её заголовка до следующего
    заголовка того же уровня или до конца текста. Пустая строка, если такой
    секции в саммари нет вовсе.
    """
    match = _REQUISITES_HEADING_RE.search(summary_md)
    if not match:
        return ""
    start = match.end()
    next_heading = _NEXT_HEADING_RE.search(summary_md, start)
    end = next_heading.start() if next_heading else len(summary_md)
    return summary_md[start:end]


def _extract_requisites(summary_md: str) -> dict[str, str] | None:
    """
    Вытащить реквизиты (вид, номер, дата, орган) из секции "1. Реквизиты".

    Строки секции — маркированный список вида "- **Подпись:** значение"
    (см. живой прогон). Подпись матчим по ключевому слову ("вид" внутри
    "Вид акта", "орган" внутри "Принявший орган" и т.п.), не по точному
    тексту — промпт саммари не фиксирует формулировку подписи, только смысл
    раздела. Если хоть один из четырёх реквизитов не нашёлся — возвращаем
    None: разобрать не удалось, шапку рисовать не из чего (см. docstring
    модуля и _render_map_sheet).
    """
    section = _requisites_section(summary_md)
    if not section:
        return None

    fields: dict[str, str] = {}
    for line in section.splitlines():
        m = _BULLET_FIELD_RE.match(line.strip())
        if not m:
            continue
        label, value = m.group(1).strip().lower(), m.group(2).strip()
        if not value:
            continue
        for keyword, key in _REQUISITE_KEYWORDS.items():
            if keyword in label and key not in fields:
                fields[key] = value
                break

    if not all(key in fields for key in _REQUISITE_KEYWORDS.values()):
        return None
    return fields


def _summary_header_line(summary_md: str) -> str | None:
    """
    Собрать одну осмысленную строку шапки листа карты из реквизитов саммари
    (вид, номер, дата, орган). None, если реквизиты разобрать не удалось —
    тогда шапки на листе не будет вовсе, лист начнётся сразу с заголовков
    колонок (мусорная шапка хуже отсутствующей, см. правку владельца
    2026-08-24).
    """
    fields = _extract_requisites(summary_md)
    if fields is None:
        return None
    return f"{fields['вид']} {fields['номер']} от {fields['дата']} ({fields['орган']})"


def _row_to_request_nums(request: DataRequest) -> dict[int, list[str]]:
    """
    row_id строки карты -> номера показателей запроса, которые на неё ссылаются
    (RequestItem.for_rows). Основа обратных ссылок в колонке «Данные».
    """
    mapping: dict[int, list[str]] = {}
    for item in request.items:
        for row_id in item.for_rows:
            mapping.setdefault(row_id, []).append(item.num)
    return mapping


def _render_effect_cell(effect: Effect) -> str:
    """Показатель эффекта и способ его расчёта в одной клетке, каждый на своей строке."""
    return f"{effect.metric}\n{effect.how_to_calculate}"


def _render_map_data_cell(data: list[DataItem], nums: list[str]) -> str:
    """
    Человекочитаемый рендер показателей строки карты для колонки «Данные».

    Показателям с источником «запрос органу» дописывается обратная ссылка —
    номера строк листа «Запрос показателей» (см. _row_to_request_nums), по
    образцу рендера данных в build_excel.py фазы 2.
    """
    lines = []
    for dp in data:
        parts = [dp.source]
        if dp.where:
            parts.append(dp.where.strip())
        if dp.source == "запрос органу" and nums:
            parts.append("№ " + ", ".join(nums))
        lines.append(f"{dp.metric} ({', '.join(parts)})")
    return "\n".join(lines)


def _format_map_sheet(ws: Worksheet, header_row: int) -> None:
    """Ширины колонок, шапка (с учётом строки реквизитов), перенос текста, рамки."""
    for col, width in {"A": 14, "B": 46, "C": 40, "D": 52, "E": 40, "F": 10}.items():
        ws.column_dimensions[col].width = width
    _style_header_row(ws, header_row)
    for row in ws.iter_rows(min_row=header_row + 1, max_col=6):
        for c in row:
            c.font = _BODY_FONT
            c.alignment = _WRAP_TOP
            c.border = _BORDER


def _render_map_sheet(
    wb: Workbook, hyp_map: HypothesisMap, row_to_nums: dict[int, list[str]], summary_md: str,
) -> None:
    """Лист «Карта гипотез»: опциональная шапка-реквизиты (одна строка) + 6 колонок карты."""
    ws = wb.active
    ws.title = _SHEET_MAP

    header_line = _summary_header_line(summary_md)
    has_header_line = header_line is not None
    if has_header_line:
        ws.append([header_line])

    header_row = 2 if has_header_line else 1
    ws.append(["Индикаторы", "Гипотеза", "Эффект", "Данные", "Пруф", "Пункт"])

    for row in hyp_map.rows:
        nums = row_to_nums.get(row.row_id, [])
        ws.append([
            ", ".join(str(i) for i in row.indicators),
            row.hypothesis,
            _render_effect_cell(row.effect),
            _render_map_data_cell(row.data, nums),
            row.proof or "",
            row.clause or "",
        ])

    _format_map_sheet(ws, header_row)


def _format_request_sheet(ws: Worksheet) -> None:
    """Ширины колонок, шапка, оформление тела (образец build_excel.py фазы 2)."""
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 60
    for col in ("C", "D", "E"):
        ws.column_dimensions[col].width = 12
    _style_header_row(ws, 1)
    for row in ws.iter_rows(min_row=2, max_col=5):
        row[0].font = _BODY_FONT
        row[0].alignment = _CENTER_TOP
        row[0].border = _BORDER
        row[1].font = _BODY_FONT
        row[1].alignment = _WRAP_TOP
        row[1].border = _BORDER
        for c in row[2:]:
            c.border = _BORDER
            c.alignment = _CENTER


def _render_request_sheet(wb: Workbook, request: DataRequest, years: list[int]) -> None:
    """
    Лист «Запрос показателей»: № | Показатель | три года (пустые клетки).

    Разрезы (breakdown) — подстроками под показателем: основная строка «1»,
    подстроки «1.1», «1.2» (образец эталона Постаматы), а не текстом внутри
    одной клетки.
    """
    ws = wb.create_sheet(_SHEET_REQUEST)
    ws.append(["№", "Показатель"] + [str(y) for y in years])

    for item in request.items:
        ws.append([item.num, item.metric, None, None, None])
        for i, breakdown in enumerate(item.breakdown, start=1):
            ws.append([f"{item.num}.{i}", breakdown, None, None, None])

    _format_request_sheet(ws)


def _indicator_label(v: Verdict) -> str:
    """Номер и имя индикатора в одной клетке — «7. отсутствие избыточных процедур»."""
    return f"{v.indicator}. {v.name}"


def _format_listing_sheet(ws: Worksheet, widths: dict[str, int]) -> None:
    """Общее оформление для листов-куч (Проверенные индикаторы / Ошибки прогона)."""
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    _style_header_row(ws, 1)
    for row in ws.iter_rows(min_row=2, max_col=len(widths)):
        for c in row:
            c.font = _BODY_FONT
            c.alignment = _WRAP_TOP
            c.border = _BORDER


def _render_checked_sheet(wb: Workbook, checked: list[Verdict]) -> None:
    """Лист «Проверенные индикаторы» из buckets.checked (соблюдён / не применимо)."""
    ws = wb.create_sheet(_SHEET_CHECKED)
    ws.append(["Индикатор", "Вердикт", "Обоснование", "Пруф", "Пункт"])
    for v in checked:
        ws.append([_indicator_label(v), v.verdict, v.rationale, v.proof or "", v.clause or ""])
    _format_listing_sheet(ws, {"A": 34, "B": 16, "C": 60, "D": 46, "E": 10})


def _render_failed_sheet(wb: Workbook, failed: list[Verdict]) -> None:
    """
    Лист «Ошибки прогона» из buckets.failed — создаётся всегда. Пустая куча
    не значит пустой лист: аналитик должен видеть, что прогон отработал целиком.
    """
    ws = wb.create_sheet(_SHEET_FAILED)
    ws.append(["Индикатор", "Что случилось", "Попыток"])
    if not failed:
        ws.append(["ошибок нет", "", ""])
    else:
        for v in failed:
            ws.append([_indicator_label(v), v.rationale, v.attempts])
    _format_listing_sheet(ws, {"A": 34, "B": 60, "C": 10})


def build_excel(
    hypothesis_map: HypothesisMap,
    request: DataRequest,
    buckets: Buckets,
    summary_md: str,
    run_id: str,
) -> Path:
    """
    Узел [7] — собрать единый .xlsx из готовых карты, запроса и куч вердиктов.

    Модель не используется. Сохраняет книгу как 08_result.xlsx в папке прогона
    и возвращает путь к файлу. Годы запроса считаются детерминированно через
    compute_years(REQUEST_YEARS_DEPTH), а не берутся из ответа модели узла [6].
    """
    logger = get_run_logger(run_id).getChild("08_excel")

    years = compute_years(num_years=REQUEST_YEARS_DEPTH)
    row_to_nums = _row_to_request_nums(request)

    wb = Workbook()
    _render_map_sheet(wb, hypothesis_map, row_to_nums, summary_md)
    _render_request_sheet(wb, request, years)
    _render_checked_sheet(wb, buckets.checked)
    _render_failed_sheet(wb, buckets.failed)

    out_path = run_dir(run_id) / "08_result.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)  # папка прогона может ещё не существовать
    wb.save(out_path)

    logger.info(
        "excel: rows=%d request_items=%d checked=%d failed=%d path=%s",
        len(hypothesis_map.rows), len(request.items), len(buckets.checked),
        len(buckets.failed), out_path,
    )
    return out_path
