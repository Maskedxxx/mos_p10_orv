"""
Узел [7] пайплайна П-10 — детерминированная сборка итогового .xlsx (app/nodes/excel.py).

Модель не используется — чистый код, как и в фазе 2 (см. build_excel.py той
версии, откуда переиспользованы стили оформления и compute_years). Собирает
книгу с четырьмя листами:

    Карта гипотез          — №, индикаторы, гипотеза, эффект, данные, на чём основано
    Запрос показателей     — №, показатель, откуда запрос, пункт акта, три года (пустые)
    Нарушений не найдено   — куча buckets.checked: индикатор, вердикт, обоснование, на чём основано
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

from app.config import REQUEST_YEARS_DEPTH, all_indicators, get_indicator
from app.contracts import Buckets, DataItem, DataRequest, Effect, HypothesisMap, RequestItem, Verdict
from app.logger import get_run_logger
from app.trace import run_dir

# --- единые стили оформления (перенесены из build_excel.py фазы 2 как есть) ---
_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")          # тёмно-синяя шапка
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)        # белый жирный
_BODY_FONT = Font(size=11)
_CAPTION_FONT = Font(size=10, italic=True, color="595959")     # подпись листа
_WRAP_TOP = Alignment(wrap_text=True, vertical="top")          # перенос, верх
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_CENTER_TOP = Alignment(horizontal="center", vertical="top")
_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

_SHEET_MAP = "Карта гипотез"
_SHEET_REQUEST = "Запрос показателей"
_SHEET_CHECKED = "Нарушений не найдено"   # было «Проверенные индикаторы»: имя не
                                          # говорило, что здесь как раз те индикаторы,
                                          # по которым гипотезы НЕ строились
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


def _render_caption(ws: Worksheet, text: str) -> None:
    """
    Одна строка-подпись над шапкой: что за индикаторы на листе и по какому
    принципу отобраны (фикс N 7.1 трека багов).

    В имя листа смысл не влезает — Excel режет имена на 31 знаке, а владелец,
    глядя на готовый файл, не понял, зачем лист «Проверенные индикаторы»
    вообще нужен. Текст кладётся только в колонку A, соседние клетки строки
    остаются пустыми, поэтому Excel даёт ему растечься по ширине листа —
    объединять ячейки не нужно.
    """
    ws.append([text])
    ws.cell(row=ws.max_row, column=1).font = _CAPTION_FONT


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


# Номер пункта модель пишет и как «4», и как «п. 2.2, 2.3, 4-6» — свой префикс
# в подписи клетки добавляем сами, поэтому чужой снимаем, чтобы не вышло
# «Норма акта, п. п. 2.2».
_CLAUSE_PREFIX_RE = re.compile(r"^\s*(?:п\.?|пункт[а-я]*)\s*", re.IGNORECASE)


def _render_basis_cell(proof: str | None, clause: str | None, sources: list[str]) -> str:
    """
    Клетка «На чём основано»: норма акта с номером пункта и внешние источники
    (фикс N 5 трека багов).

    Заменяет пару колонок «Пруф» / «Пункт»: заголовок «Пруф» — жаргон, он не
    сообщал, что в клетке дословная цитата из проверяемого акта, а «Пункт» не
    сообщал, что это её адрес внутри акта.

    Пустой цитаты не бывает: если её нет, клетка прямо говорит почему. Пустая
    клетка при заполненном пункте читалась как нарушение без обоснования —
    ровно так выглядел индикатор 10 живого прогона, у которого обоснованием
    была публикация в интернете.
    """
    head = "Норма акта"
    if clause:
        cleaned = _CLAUSE_PREFIX_RE.sub("", clause).strip()
        if cleaned:
            head += f", п. {cleaned}"

    if proof:
        body = f"«{proof}»"
    elif sources:
        body = "цитата не приведена — вывод построен на внешних источниках, а не на тексте акта"
    else:
        body = "цитата из акта не приведена"

    blocks = [f"{head}:\n{body}"]
    if sources:
        blocks.append("\n".join(["Внешние источники:"] + [f"— {s}" for s in sources]))
    return "\n\n".join(blocks)


def _verdicts_by_indicator(buckets: Buckets) -> dict[int, Verdict]:
    """Все вердикты прогона по номеру индикатора — четыре кучи в один индекс."""
    everything = buckets.violated + buckets.no_data + buckets.checked + buckets.failed
    return {v.indicator: v for v in everything}


def _row_verdicts(row_indicators: list[int], by_indicator: dict[int, Verdict]) -> list[Verdict]:
    """Вердикты индикаторов одной строки карты, в порядке самих индикаторов."""
    return [by_indicator[n] for n in row_indicators if n in by_indicator]


def _dedup_sources(verdicts: list[Verdict]) -> list[str]:
    """
    Адреса из вердиктов строки карты, без дублей, с сохранением порядка.

    Источники принадлежат вердикту индикатора, а не отдельной гипотезе: у
    одного вердикта бывает несколько гипотез, адреса у них одни и те же.
    Поэтому их переносит код, а не модель — протаскивать адреса через узлы
    [4а]/[4б] и [5] значит дважды рискнуть, что модель их потеряет или
    перепишет. Контракты Hypothesis и MapRow поля sources не имеют вовсе.
    """
    out: list[str] = []
    seen: set[str] = set()
    for v in verdicts:
        for s in v.sources:
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _first_verdict_proof(verdicts: list[Verdict]) -> str | None:
    """
    Первая непустая цитата среди вердиктов строки — подстановка, когда своей
    цитаты у строки карты нет.

    Не про запас: из семи строк живого прогона 20260825_093039_129668 три шли
    без цитаты, и у одной из трёх (индикатор 11) цитата у вердикта была —
    гипотеза её потеряла. Номер пункта так НЕ подставляется: гипотеза законно
    затрагивает больше пунктов, чем вердикт.
    """
    for v in verdicts:
        if v.proof:
            return v.proof
    return None


def _indicators_label(numbers: list[int]) -> str:
    """
    Номера и имена индикаторов строки карты, каждый на своей строке клетки.

    Имя тянется из конфига по номеру: строка карты хранит только номера
    (MapRow.indicators), имён в ней нет — в отличие от вердикта, откуда их
    берёт _indicator_label для листа проверенных индикаторов. Формат тот же,
    чтобы оформление на обоих листах совпадало (фикс N 1 трека багов).
    """
    return "\n".join(f"{n}. {get_indicator(n).name}" for n in numbers)


def _render_effect_cell(effects: list[Effect]) -> str:
    """
    Эффекты гипотезы в одной клетке: блок на каждый эффект — кого задевает,
    показатель, способ расчёта, каждый под своей подписью (фикс N 4 трека
    багов: без подписей блоки читались как один длинный абзац).

    Контракт v2 (2026-09-04): у гипотезы несколько эффектов — по одному на
    группу-плательщика («бизнес» и «город» от одного механизма), поэтому блоки
    идут друг за другом через пустую строку.

    Нумерация внутри «Как считать» остаётся такой, как её вернула модель —
    одним абзацем. Резать её кодом по «1)» / «2)» нельзя: формулировка каждый
    раз разная, это было бы гадание.
    """
    blocks = [
        f"Кого задевает: {e.affected}\n"
        f"Показатель:\n{e.metric}\n"
        f"\n"
        f"Как считать:\n{e.how_to_calculate}"
        for e in effects
    ]
    return "\n\n".join(blocks)


def _render_map_data_cell(data: list[DataItem], nums: list[str]) -> str:
    """
    Показатели строки карты двумя подписанными группами (фиксы N 2 и N 3).

    Сверху то, что запрашивают у органа, ниже открытые источники с пояснением,
    что их берут сами. Порядок ВНУТРИ группы не трогаем — он от модели.
    Пустая группа не печатается вовсе. Маркер — тире, а не цифры: цифры путались
    бы с номерами листа «Запрос показателей».

    Номера строк запроса стоят в ЗАГОЛОВКЕ блока, а не у каждого показателя.
    Причина не косметическая: связь в данных существует только на уровне строки
    карты (RequestItem.for_rows -> MapRow.row_id), до конкретного показателя её
    нет. Сопоставить по тексту нельзя — на прогоне 20260825_093039_129668 узел
    [6] переформулировал все показатели, точных совпадений 0 из 20. Раньше код
    приписывал каждому показателю весь список номеров строки, и оба показателя
    получали «N 1, 2» — сноска указывала в никуда.
    """
    internal = [dp for dp in data if dp.source == "запрос органу"]
    external = [dp for dp in data if dp.source != "запрос органу"]

    blocks: list[str] = []

    if internal:
        head = "Запрос в орган"
        if nums:
            head += f" (лист «{_SHEET_REQUEST}», № {', '.join(nums)})"
        blocks.append("\n".join([f"{head}:"] + [f"— {dp.metric}" for dp in internal]))

    if external:
        lines = ["Открытые источники (берём сами, у органа не запрашиваем):"]
        for dp in external:
            lines.append(f"— {dp.metric}")
            if dp.where:
                lines.append(f"  — {dp.where.strip()}")   # адрес источника подстрокой
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def _format_map_sheet(ws: Worksheet, header_row: int) -> None:
    """Ширины колонок, шапка (с учётом строки реквизитов), перенос текста, рамки."""
    for col, width in {"A": 6, "B": 34, "C": 46, "D": 40, "E": 52, "F": 52}.items():
        ws.column_dimensions[col].width = width
    _style_header_row(ws, header_row)
    for row in ws.iter_rows(min_row=header_row + 1, max_col=6):
        row[0].font = _BODY_FONT
        row[0].alignment = _CENTER_TOP        # номер строки — по центру, как № запроса
        row[0].border = _BORDER
        for c in row[1:]:
            c.font = _BODY_FONT
            c.alignment = _WRAP_TOP
            c.border = _BORDER


def _unverified_verdicts(hyp_map: HypothesisMap, buckets: Buckets) -> list[Verdict]:
    """
    Вердикты «нет данных», не представленные ни одной строкой карты — проверка
    индикатора не завершена (ОС экспертов, Б1).

    Так бывает, когда узел [4б] для индикатора не вызывался (семейство «текст /
    правовая база», см. config.NO_DATA_TEXT_FAMILY) либо вернул пустой список
    гипотез. Раньше такой вердикт не попадал ни на один лист, и аналитик не знал,
    что проверка не доведена до конца.
    """
    covered = {n for row in hyp_map.rows for n in row.indicators}
    return [v for v in buckets.no_data if v.indicator not in covered]


def _append_unverified_rows(ws: Worksheet, hyp_map: HypothesisMap, buckets: Buckets) -> None:
    """
    Дописать в карту строки «проверка не завершена» — по одной на вердикт из
    _unverified_verdicts. Нумерация продолжает строки карты; в запрос органу
    такие строки не идут — дочитывает аналитик по правовой базе.
    """
    next_id = max((row.row_id for row in hyp_map.rows), default=0) + 1
    for v in _unverified_verdicts(hyp_map, buckets):
        ws.append([
            next_id,
            _indicators_label([v.indicator]),
            "Проверка не завершена — не хватило текста или правовой базы.\n\n"
            f"Что не проверено:\n{v.missing_data or 'не указано'}\n\n"
            f"Как проверял агент:\n{v.rationale}",
            "—",
            "Дочитать самостоятельно по правовой базе. В запрос органу не идёт.",
            _render_basis_cell(v.proof, v.clause, v.sources),
        ])
        next_id += 1


def _render_map_sheet(
    wb: Workbook, hyp_map: HypothesisMap, row_to_nums: dict[int, list[str]], summary_md: str,
    buckets: Buckets, caption: str,
) -> None:
    """
    Лист «Карта гипотез»: опциональная шапка-реквизиты (одна строка) + 5 колонок.

    buckets нужны для колонки «На чём основано»: внешние источники и запасная
    цитата лежат в вердиктах, а не в строке карты (см. _dedup_sources).
    """
    ws = wb.active
    ws.title = _SHEET_MAP

    header_line = _summary_header_line(summary_md)
    if header_line is not None:
        ws.append([header_line])          # реквизиты акта — самая первая строка
    _render_caption(ws, caption)

    header_row = ws.max_row + 1
    ws.append(["№", "Индикаторы", "Гипотеза", "Эффект", "Данные", "На чём основано"])

    by_indicator = _verdicts_by_indicator(buckets)
    for row in hyp_map.rows:
        nums = row_to_nums.get(row.row_id, [])
        verdicts = _row_verdicts(row.indicators, by_indicator)
        ws.append([
            row.row_id,          # адрес строки: на него ссылается лист запроса
            _indicators_label(row.indicators),
            row.hypothesis,
            _render_effect_cell(row.effects),
            _render_map_data_cell(row.data, nums),
            _render_basis_cell(
                row.proof or _first_verdict_proof(verdicts),
                row.clause,                       # пункт карты вердиктом не подменяется
                _dedup_sources(verdicts),
            ),
        ])

    _append_unverified_rows(ws, hyp_map, buckets)   # «нет данных» без гипотез — тоже в карту
    _format_map_sheet(ws, header_row)


def _format_request_sheet(ws: Worksheet, header_row: int) -> None:
    """Ширины колонок, шапка, оформление тела (образец build_excel.py фазы 2)."""
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 60
    ws.column_dimensions["C"].width = 26      # «Откуда запрос» — ссылки на карту
    ws.column_dimensions["D"].width = 14      # «Пункт акта»
    for col in ("E", "F", "G"):
        ws.column_dimensions[col].width = 12
    _style_header_row(ws, header_row)
    for row in ws.iter_rows(min_row=header_row + 1, max_col=7):
        row[0].font = _BODY_FONT
        row[0].alignment = _CENTER_TOP
        row[0].border = _BORDER
        for c in row[1:3]:                    # показатель и ссылки — с переносом
            c.font = _BODY_FONT
            c.alignment = _WRAP_TOP
            c.border = _BORDER
        for c in row[3:]:
            c.border = _BORDER
            c.alignment = _CENTER


def _render_source_rows_cell(for_rows: list[int], row_indicators: dict[int, list[int]]) -> str:
    """
    Обратная ссылка на карту: «стр. N — инд. M», каждая на своей строке клетки.

    Получив от органа цифры, аналитик должен видеть, к какой гипотезе и какому
    индикатору они относятся, не перебирая карту вручную (фикс N 6). Номера
    строк уже посчитаны узлом [6] в RequestItem.for_rows, индикаторы берутся
    из самой карты по row_id.
    """
    lines = []
    for row_id in for_rows:
        nums = row_indicators.get(row_id)
        if nums is None:
            continue                          # ссылка в никуда — узел [6] её уже залогировал
        lines.append(f"стр. {row_id} — инд. {', '.join(str(n) for n in nums)}")
    return "\n".join(lines)


_KIND_LIST = "перечень или реестр"


def _request_metric_label(item: RequestItem) -> str:
    """
    Текст показателя для клетки бланка: формулировка + единица измерения через
    запятую («число заявок (пункт 5.1.4), ед.»). Единица — отдельное поле контракта
    (ОС экспертов 2026-09-04, Г1: одна позиция — одна величина); у перечней её нет.
    """
    label = f"{item.metric}, {item.unit}" if item.unit else item.metric
    if item.note:
        # решение владельца 2026-09-04: похожие позиции остаются обе, но с пояснением,
        # чем отличаются — чтобы эксперт не спрашивал «а в чём отличие от предыдущего?»
        label += f"\nПояснение: {item.note}"
    return label


def _render_request_sheet(
    wb: Workbook, request: DataRequest, years: list[int], hyp_map: HypothesisMap,
    caption: str,
) -> None:
    """
    Лист «Запрос показателей»: № | Показатель | три года (пустые клетки).

    Разрезы (breakdown) — подстроками под показателем: основная строка «1»,
    подстроки «1.1», «1.2» (образец эталона Постаматы), а не текстом внутри
    одной клетки.
    """
    ws = wb.create_sheet(_SHEET_REQUEST)
    _render_caption(ws, caption)
    header_row = ws.max_row + 1
    ws.append(["№", "Показатель", "Откуда запрос", "Пункт акта"] + [str(y) for y in years])

    row_indicators = {row.row_id: row.indicators for row in hyp_map.rows}
    empty_years = [None] * len(years)
    dash_years = ["—"] * len(years)          # у перечня клеток годов нет (Г2)

    def _append_item(item, year_cells) -> None:
        ws.append([
            item.num, _request_metric_label(item),
            _render_source_rows_cell(item.for_rows, row_indicators),
            item.clause or "",
            *year_cells,
        ])
        # подстроки разрезов ссылок не несут — они принадлежат родительской строке
        for i, breakdown in enumerate(item.breakdown, start=1):
            ws.append([f"{item.num}.{i}", breakdown, "", "", *year_cells])

    counters = [it for it in request.items if it.kind != _KIND_LIST]
    lists = [it for it in request.items if it.kind == _KIND_LIST]
    for item in counters:
        _append_item(item, empty_years)
    if lists:
        # ОС экспертов (2026-09-04, Г2): «перечень — это надо не в этой таблице»,
        # «должно быть отдельным реестром» — перечни идут своим блоком, без годов
        ws.append([
            "", "Перечни и реестры — заполняются отдельным документом, без разбивки по годам",
            "", "", *[""] * len(years),
        ])
        for item in lists:
            _append_item(item, dash_years)

    _format_request_sheet(ws, header_row)


def _indicator_label(v: Verdict) -> str:
    """Номер и имя индикатора в одной клетке — «7. отсутствие избыточных процедур»."""
    return f"{v.indicator}. {v.name}"


def _format_listing_sheet(ws: Worksheet, widths: dict[str, int], header_row: int) -> None:
    """Общее оформление для листов-куч (Нарушений не найдено / Ошибки прогона)."""
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    _style_header_row(ws, header_row)
    for row in ws.iter_rows(min_row=header_row + 1, max_col=len(widths)):
        for c in row:
            c.font = _BODY_FONT
            c.alignment = _WRAP_TOP
            c.border = _BORDER


def _render_checked_sheet(wb: Workbook, checked: list[Verdict], caption: str) -> None:
    """Лист «Нарушений не найдено» из buckets.checked (соблюдён / не применимо)."""
    ws = wb.create_sheet(_SHEET_CHECKED)
    _render_caption(ws, caption)
    header_row = ws.max_row + 1
    ws.append(["Индикатор", "Вердикт", "Обоснование", "На чём основано"])
    for v in checked:
        # «Обоснование» — текст вердикта, «На чём основано» — цитата и адреса;
        # рендер общий с листом карты, чтобы одно и то же выглядело одинаково
        ws.append([
            _indicator_label(v), v.verdict, v.rationale,
            _render_basis_cell(v.proof, v.clause, list(v.sources)),
        ])
    _format_listing_sheet(ws, {"A": 34, "B": 16, "C": 60, "D": 52}, header_row)


def _render_failed_sheet(wb: Workbook, failed: list[Verdict], caption: str) -> None:
    """
    Лист «Ошибки прогона» из buckets.failed — создаётся всегда. Пустая куча
    не значит пустой лист: аналитик должен видеть, что прогон отработал целиком.
    """
    ws = wb.create_sheet(_SHEET_FAILED)
    _render_caption(ws, caption)
    header_row = ws.max_row + 1
    ws.append(["Индикатор", "Что случилось", "Попыток"])
    if not failed:
        ws.append(["ошибок нет", "", ""])
    else:
        for v in failed:
            ws.append([_indicator_label(v), v.rationale, v.attempts])
    _format_listing_sheet(ws, {"A": 34, "B": 60, "C": 10}, header_row)


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

    # Числитель подписей считается по факту прогона, знаменатель — из конфига:
    # 12 индикаторов задаёт методика, зашивать это число в текст нельзя.
    total = len(all_indicators())
    map_indicators = {n for row in hypothesis_map.rows for n in row.indicators}
    map_indicators |= {v.indicator for v in _unverified_verdicts(hypothesis_map, buckets)}

    wb = Workbook()
    _render_map_sheet(
        wb, hypothesis_map, row_to_nums, summary_md, buckets,
        f"Индикаторы, по которым найдено нарушение или не хватило данных — "
        f"{len(map_indicators)} из {total}. По каждому построена гипотеза: "
        f"что не так, чем мерить эффект, какие данные нужны — либо отмечено, "
        f"что проверка не завершена.",
    )
    _render_request_sheet(
        wb, request, years, hypothesis_map,
        "Данные, которых нет в открытом доступе. Заполняет орган-разработчик, "
        "три последних завершённых года.",
    )
    _render_checked_sheet(
        wb, buckets.checked,
        f"Индикаторы, по которым гипотезы не строились — {len(buckets.checked)} из {total}. "
        f"Вердикт «соблюдён» или «не применимо», с обоснованием и цитатой из акта.",
    )
    _render_failed_sheet(
        wb, buckets.failed,
        f"Индикаторы, проверка которых не завершилась. "
        f"Пусто — значит все {total} отработали.",
    )

    out_path = run_dir(run_id) / "08_result.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)  # папка прогона может ещё не существовать
    wb.save(out_path)

    logger.info(
        "excel: rows=%d request_items=%d checked=%d failed=%d path=%s",
        len(hypothesis_map.rows), len(request.items), len(buckets.checked),
        len(buckets.failed), out_path,
    )
    return out_path
