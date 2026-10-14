"""
Узел [7] пайплайна П-10 — детерминированная сборка итогового .xlsx (app/nodes/excel.py).

Модель не используется — чистый код, как и в фазе 2 (см. build_excel.py той
версии, откуда переиспользованы стили оформления и compute_years). Собирает
книгу с пятью листами:

    Как заполнять          — инструкция эксперту: акт и дата от сервиса, ФИО и дата отзыва — оранжевые
    Карта гипотез          — №, индикаторы, гипотеза, эффект (кого задевает, показатель,
                             как считать, что подтвердит гипотезу), данные, на чём основано
    Запрос показателей     — №, показатель, откуда запрос, пункт акта, три года (пустые)
    Вердикты               — все вердикты четырёх куч: индикатор, вердикт, обоснование, на чём основано, где подробнее
    Ошибки прогона         — куча buckets.failed: индикатор, что случилось, попыток

На первых трёх листах справа — оранжевые колонки для оценки экспертами
(пул 3, п. 6.1 письма заказчика 2026-09-04), см. _append_expert_columns.

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
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from app.config import REQUEST_YEARS_DEPTH, all_indicators, get_indicator
from app.contracts import (
    ActPassport, Buckets, DataItem, DataRequest, Effect, HypothesisMap, JournalEntry, Marks, RequestItem, Verdict,
)
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
_EXPERT_FILL = PatternFill("solid", fgColor="FFF2CC")   # клетки для оценки экспертами (пул 3, п. 6.1)
# 2.7 (итерация 4, В): строки «нет данных» на листе «Вердикты» — эксперту нужна доп. проверка
_NO_DATA_FILL = PatternFill("solid", fgColor="FDE9D9")

_SHEET_HOWTO = "Как заполнять"   # лист-инструкция для эксперта, первый в книге (пул 3, 6.2)
_SHEET_MAP = "Карта гипотез"
_SHEET_REQUEST = "Запрос показателей"
_SHEET_QUESTIONS = "Вопросы органу"   # 2.7 (итерация 4, Ж2): вопросы словами — отдельным бланком
_SHEET_VERDICTS = "Вердикты"   # пул 3 (письмо заказчика 2026-09-04, п. 4): на листе
                               # ВСЕ индикаторы, не только соблюдённые; имя — из письма.
                               # История: «Проверенные индикаторы» → «Нарушений не найдено» (7.1)
_SHEET_FAILED = "Ошибки прогона"
_SHEET_JOURNAL = "Журнал изменений"   # только в версиях диалогового режима (план итерации 3, шаг 1.5)
_MARKS_HEADER = "Изменения"           # колонка пометок строк — тоже только в версиях


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


def _passport_header_line(passport: ActPassport | None) -> str | None:
    """
    Шапка листа карты — одна строка реквизитов из паспорта акта:
    «приказ N 78-15-3/24 от 16 января 2024 г. (МАДИ)». У проекта номера и даты
    нет — остаются вид, «проект» и орган. Без паспорта шапки нет (правка
    владельца 2026-08-24: мусорная шапка хуже отсутствующей).
    """
    if passport is None:
        return None
    r = passport.requisites
    parts = [r.kind]
    if passport.status.status == "проект":
        parts.append("(проект)")
    if r.number:
        parts.append(r.number)
    if r.date:
        parts.append(f"от {r.date}")
    line = " ".join(parts) + f" ({r.authority})"
    if r.developers:
        # 2.7 (А): разработчик — откуда взят: выбор пользователя или пункты акта, по которым определён
        line += ". Разработчик: " + "; ".join(_developer_label(d) for d in r.developers)
    return line


def _developer_label(d) -> str:
    """«Департамент … (приложение 1; определено моделью по п. 1.5)» или «… (указан пользователем)»."""
    if d.basis == "указан пользователем при загрузке":
        how = "указан пользователем"
    else:
        how = f"определено моделью по {d.clauses}" if d.clauses else "определено моделью"
    scope = f"{d.scope}; " if d.scope and d.scope != "весь акт" else ""
    return f"{d.name} ({scope}{how})"


_KIND_QUESTION = "открытый вопрос"


def _row_to_request_nums(request: DataRequest, *, questions: bool = False) -> dict[int, list[str]]:
    """
    row_id строки карты -> номера позиций запроса, которые на неё ссылаются
    (RequestItem.for_rows). Основа обратных ссылок в колонке «Данные».

    questions=False — позиции листа «Запрос показателей» (цифры и перечни),
    True — открытые вопросы листа «Вопросы органу» (2.7, Ж2).
    """
    mapping: dict[int, list[str]] = {}
    for item in request.items:
        if (item.kind == _KIND_QUESTION) != questions:
            continue
        for row_id in item.for_rows:
            mapping.setdefault(row_id, []).append(item.num)
    return mapping


# Номер пункта модель пишет и как «4», и как «п. 2.2, 2.3, 4-6» — свой префикс
# в подписи клетки добавляем сами, поэтому чужой снимаем, чтобы не вышло
# «Норма акта, п. п. 2.2».
_CLAUSE_PREFIX_RE = re.compile(r"^\s*(?:п\.?|пункт[а-я]*)\s*", re.IGNORECASE)


def _points(brief: list[str] | None, full: str) -> str:
    """
    Текст для эксперта (2.5): выжимка пунктами, каждый с новой строки. Полный текст
    агента остаётся в JSON прогона для анализа; в Excel он идёт, только если выжимки
    нет — прогон до 2.5 или объект, собранный кодом без выжимки.
    """
    return "\n".join(f"• {p}" for p in brief) if brief else full


def _render_basis_cell(
    proof: str | None, clause: str | None, sources: list[str], grounds: str | None = None,
) -> str:
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
    if grounds:
        # контракт v2: на чём стоит гипотеза — дословно из карточки вердикта
        blocks.append(f"Основание из вердикта:\n{grounds}")
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

    Четвёртая подпись «Что подтвердит гипотезу» — пул 3 (письмо заказчика
    2026-09-04, п. 1б): «какую динамику показателя необходимо зафиксировать
    (рост/убыль)».
    """
    blocks = [
        f"Кого задевает: {e.affected}\n"
        f"Показатель:\n{e.metric}\n"
        f"\n"
        f"Как считать:\n{_points(e.how_to_calculate_brief, e.how_to_calculate)}\n"
        f"\n"
        f"Что подтвердит гипотезу:\n{_points(e.expected_trend_brief, e.expected_trend)}"
        for e in effects
    ]
    return "\n\n".join(blocks)


def _render_map_data_cell(data: list[DataItem], nums: list[str], question_nums: list[str] | None = None) -> str:
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
    # 2.7 (итерация 4): ещё две группы — вопросы органу словами (Ж2, свой лист бланка)
    # и цифры, которые по слову эксперта нигде не ведутся (Ж3, ни в запрос, ни в открытые)
    internal = [dp for dp in data if dp.source == "запрос органу"]
    questions = [dp for dp in data if dp.source == "вопрос органу"]
    external = [dp for dp in data if dp.source == "открытый"]
    not_tracked = [dp for dp in data if dp.source == "нигде не ведётся"]

    blocks: list[str] = []

    if internal:
        head = "Запрос в орган"
        if nums:
            head += f" (лист «{_SHEET_REQUEST}», № {', '.join(nums)})"
        blocks.append("\n".join([f"{head}:"] + [f"— {dp.metric}" for dp in internal]))

    if questions:
        head = "Вопрос органу"
        if question_nums:
            head += f" (лист «{_SHEET_QUESTIONS}», № {', '.join(question_nums)})"
        blocks.append("\n".join([f"{head}:"] + [f"— {dp.metric}" for dp in questions]))

    if external:
        lines = ["Открытые источники (берём сами, у органа не запрашиваем):"]
        for dp in external:
            lines.append(f"— {dp.metric}")
            if dp.where:
                lines.append(f"  — {dp.where.strip()}")   # адрес источника подстрокой
        blocks.append("\n".join(lines))

    if not_tracked:
        blocks.append("\n".join(["Нигде не ведётся (отметил эксперт):"] + [f"— {dp.metric}" for dp in not_tracked]))

    return "\n\n".join(blocks)


_EXPERT_LISTS = {"Оценка (1–5)": '"1,2,3,4,5"', "Согласны?": '"да,нет"'}   # выпадающие списки


def _append_expert_columns(ws: Worksheet, header_row: int, names: list[str], widths: list[int]) -> None:
    """
    Дописать справа колонки для оценки экспертами: заголовок общим стилем шапки,
    тело — оранжевая заливка FFF2CC, рамка, перенос; у «Оценка (1–5)» и «Согласны?»
    выпадающий список (_EXPERT_LISTS), как в шаблоне обратной связи экспертов ОС.

    Пул 3 (письмо заказчика 2026-09-04, п. 6.1): «включить оранжевые столбцы
    (для заполнения экспертами) сразу в выгружаемый файл». Решение владельца —
    всегда, одна книга. Имена колонок и цвет — как в файлах экспертов ОС,
    которые они дорисовывали руками. Вызывать после _format_*_sheet: шапка уже
    оформлена, ширина и стиль новых колонок задаются здесь.
    """
    first = ws.max_column + 1
    for i, (name, width) in enumerate(zip(names, widths)):
        col = first + i
        head = ws.cell(row=header_row, column=col, value=name)
        head.font = _HEADER_FONT
        head.fill = _HEADER_FILL
        head.alignment = _CENTER
        head.border = _BORDER
        ws.column_dimensions[head.column_letter].width = width
        for r in range(header_row + 1, ws.max_row + 1):
            c = ws.cell(row=r, column=col)
            c.fill = _EXPERT_FILL
            c.border = _BORDER
            c.font = _BODY_FONT
            c.alignment = _WRAP_TOP
        if name in _EXPERT_LISTS and ws.max_row > header_row:
            dv = DataValidation(type="list", formula1=_EXPERT_LISTS[name], allow_blank=True)
            dv.add(f"{head.column_letter}{header_row + 1}:{head.column_letter}{ws.max_row}")
            ws.add_data_validation(dv)


def _append_marks_column(ws: Worksheet, header_row: int, key_of, edited, stale: dict) -> None:
    """
    Колонка «Изменения» — только в версиях диалогового режима (план итерации 3,
    шаг 1.5): «правлено человеком» и «устарело: почему» у своей строки. Ставится
    перед оранжевыми колонками эксперта. key_of превращает значение первой
    колонки строки в ключ пометки; строки без ключа (разрезы, заголовок перечней)
    остаются пустыми — пометка принадлежит строке-родителю.
    """
    col = ws.max_column + 1
    head = ws.cell(row=header_row, column=col, value=_MARKS_HEADER)
    head.font, head.fill, head.alignment, head.border = _HEADER_FONT, _HEADER_FILL, _CENTER, _BORDER
    ws.column_dimensions[head.column_letter].width = 30
    for r in range(header_row + 1, ws.max_row + 1):
        key = key_of(ws.cell(row=r, column=1).value)
        parts = []
        if key is not None and key in edited:
            parts.append("правлено человеком")
        if key is not None and key in stale:
            parts.append(f"устарело: {stale[key]}")
        c = ws.cell(row=r, column=col, value="; ".join(parts) or None)
        c.font, c.alignment, c.border = _BODY_FONT, _WRAP_TOP, _BORDER


def _row_key(value) -> int | None:
    """Ключ строки карты — её номер (число в первой колонке)."""
    return value if isinstance(value, int) else None


def _item_key(value) -> str | None:
    """Ключ позиции запроса — её номер; разрезы «1.1» и заголовок перечней ключа не имеют."""
    return value if isinstance(value, str) and value.isdigit() else None


def _indicator_key(value) -> int | None:
    """Ключ строки листа «Вердикты» — номер индикатора из «7. название»."""
    head = str(value).split(".")[0] if value else ""
    return int(head) if head.isdigit() else None


_EXPERT_SCORE_COLUMNS = (["Оценка (1–5)", "Замечание"], [12, 40])
# 2.7 (итерация 4, В): «Где искать» — эксперт подсказывает, где лежат сведения для «нет данных»
_EXPERT_VERDICT_COLUMNS = (["Согласны?", "Если нет — какой верный", "Замечание", "Где искать"], [12, 30, 40, 40])


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


def _violated_without_hypotheses(hyp_map: HypothesisMap, buckets: Buckets) -> list[Verdict]:
    """
    14.10 (ошибка 1 аудита): вердикты «нарушен», не представленные ни одной строкой карты, — гипотезы по ним
    не построены (сбой узла гипотез). Раньше такой индикатор молча пропадал из карты и бланка; теперь он —
    строка листа «Ошибки прогона».
    """
    covered = {n for row in hyp_map.rows for n in row.indicators}
    return [
        Verdict(indicator=v.indicator, name=v.name, verdict="ошибка", attempts=v.attempts,
                rationale="Найдено нарушение, но гипотезы по нему не построены — проверьте индикатор вручную.")
        for v in buckets.violated if v.indicator not in covered
    ]


def _unverified_row_ids(
    hyp_map: HypothesisMap, buckets: Buckets, fixed: dict[int, int] | None = None,
) -> dict[int, int]:
    """
    Номер строки карты для каждого вердикта «проверка не завершена»: индикатор →
    row_id. Считается в build_excel один раз, чтобы карта и лист «Вердикты»
    ссылались на одно и то же число (пул 3, п. 4).

    fixed — номера, закреплённые версиями диалогового режима (план итерации 3,
    шаг 1.5): без них «максимум + 1» сдвигается после удаления строки карты.
    Индикаторам без закреплённого номера достаются следующие свободные.
    """
    fixed = fixed or {}
    next_id = max([row.row_id for row in hyp_map.rows] + list(fixed.values()), default=0) + 1
    ids: dict[int, int] = {}
    for v in _unverified_verdicts(hyp_map, buckets):
        if v.indicator in fixed:
            ids[v.indicator] = fixed[v.indicator]
        else:
            ids[v.indicator] = next_id
            next_id += 1
    return ids


def _append_unverified_rows(
    ws: Worksheet, hyp_map: HypothesisMap, buckets: Buckets, row_ids: dict[int, int],
) -> None:
    """
    Дописать в карту строки «Нужна ручная проверка» — по одной на вердикт из
    _unverified_verdicts. Нумерация продолжает строки карты; в запрос органу
    такие строки не идут — дочитывает аналитик по правовой базе.

    2.5: эксперты читали прежнее «Проверка не завершена» и весь ход проверки агента
    как техническую ошибку ИИ. Теперь коротко: выжимка агента «Смотрел: … / Не нашёл: …»;
    у прогонов до 2.5 — только чего не хватило (missing_data), без хода проверки.
    """
    for v in _unverified_verdicts(hyp_map, buckets):
        details = _points(v.rationale_brief, f"Не удалось проверить: {v.missing_data or 'не указано'}")
        ws.append([
            row_ids[v.indicator],
            _indicators_label([v.indicator]),
            f"Нужна ручная проверка.\n\n{details}",
            "—",
            "Дочитать самостоятельно по правовой базе. В запрос органу не идёт.",
            _render_basis_cell(v.proof, v.clause, v.sources),
        ])


_MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")


def _render_howto_sheet(
    wb: Workbook, passport: ActPassport | None,
    analyzed_at: datetime.datetime | None = None, version: int | None = None,
    act_types: list[str] | None = None,
) -> None:
    """
    Лист «Как заполнять» — первый в книге, инструкция эксперту по образцу шаблона
    обратной связи, который до пула 3 собирался отдельно от пайплайна (файлы
    экспертов ОС 2026-09-03: «Заполнено сервисом» / «Заполните вы» / шесть правил).

    Заказчик (письмо 2026-09-04, п. 6.2): в файле от сервиса нет первого листа
    «Как заполнять» и строк, заполняемых автоматически; владелец 2026-09-04 —
    сделать на время тестирования. Имена листов в правилах — те же константы,
    что у самих листов, чтобы инструкция не расходилась с книгой.
    """
    ws = wb.active
    ws.title = _SHEET_HOWTO
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 80
    # В версии диалога «Разобран» — дата исходного разбора, а не момент пересборки
    moment = analyzed_at or datetime.datetime.now()
    rendered = f"{moment.day} {_MONTHS_RU[moment.month - 1]} {moment.year}, {moment:%H:%M}"
    act = _passport_header_line(passport) or "—"
    rules = [
        f"Пройдите четыре листа: «{_SHEET_MAP}», «{_SHEET_REQUEST}», «{_SHEET_QUESTIONS}», «{_SHEET_VERDICTS}».",
        "Оранжевые столбцы справа — ваши. Синие столбцы менять не нужно.",
        f"На листах «{_SHEET_MAP}», «{_SHEET_REQUEST}» и «{_SHEET_QUESTIONS}» поставьте оценку от 1 до 5: "
        "щёлкните по ячейке и выберите значение из выпадающего списка.",
        f"На листе «{_SHEET_VERDICTS}» оценки нет. Там ответ «да» или «нет» на вопрос "
        "«Согласны?» — тоже из списка; если «нет», напишите рядом верный вердикт.",
        f"Если на листе «{_SHEET_VERDICTS}» знаете, где лежат сведения для подсвеченного индикатора "
        "(«нет данных»), — напишите это в графе «Где искать»: сервис проверит индикатор заново.",
        "Замечание пишите своими словами. Одно «неверно» нам не поможет — нужно, что именно не так.",
        "Готовый файл пришлите обратно. Имя файла не меняйте — по нему видно, о каком акте речь.",
    ]
    lines: list[tuple[str | None, str | None, str]] = [
        ("Обратная связь по разбору нормативного акта", None, "title"),
        (None, None, "blank"),
        ("Заполнено сервисом", None, "section"),
        ("Акт", act, "row"),
        ("Разобран", rendered, "row"),
        # 2.7 (АМ): по типу акта в бланк идут обязательные показатели формы
        ("Тип акта", ", ".join(act_types) if act_types else "не указан", "row"),
    ] + ([("Версия файла", f"{version} — после правок в диалоге; исходный разбор сервиса — версия 0", "row")]
         if version else []) + [
        (None, None, "blank"),
        ("Заполните вы", None, "section"),
        ("ФИО", None, "orange"),
        ("Дата отзыва", None, "orange"),
        (None, None, "blank"),
        ("Что нужно сделать", None, "section"),
    ] + [(str(i), rule, "row") for i, rule in enumerate(rules, start=1)]
    for a, b, kind in lines:
        ws.append([a, b])
        r = ws.max_row
        ca, cb = ws.cell(row=r, column=1), ws.cell(row=r, column=2)
        ca.font = Font(bold=True, size=14 if kind == "title" else 12 if kind == "section" else 11)
        cb.font = _BODY_FONT
        cb.alignment = _WRAP_TOP
        if kind == "orange":
            cb.fill = _EXPERT_FILL
            cb.border = _BORDER


def _render_map_sheet(
    wb: Workbook, hyp_map: HypothesisMap, row_to_nums: dict[int, list[str]],
    passport: ActPassport | None, buckets: Buckets, caption: str,
    unverified_ids: dict[int, int], marks: Marks | None = None,
    row_to_question_nums: dict[int, list[str]] | None = None,
) -> None:
    """
    Лист «Карта гипотез»: опциональная шапка-реквизиты (одна строка) + 5 колонок.

    buckets нужны для колонки «На чём основано»: внешние источники и запасная
    цитата лежат в вердиктах, а не в строке карты (см. _dedup_sources).
    """
    ws = wb.create_sheet(_SHEET_MAP)

    header_line = _passport_header_line(passport)
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
            _points(row.hypothesis_brief, row.hypothesis),
            _render_effect_cell(row.effects),
            _render_map_data_cell(row.data, nums, (row_to_question_nums or {}).get(row.row_id, [])),
            _render_basis_cell(
                row.proof or _first_verdict_proof(verdicts),
                row.clause,                       # пункт карты вердиктом не подменяется
                _dedup_sources(verdicts),
                _points(row.grounds_brief, row.grounds),
            ),
        ])

    _append_unverified_rows(ws, hyp_map, buckets, unverified_ids)   # «нет данных» без гипотез — тоже в карту
    _format_map_sheet(ws, header_row)
    if marks is not None:
        _append_marks_column(ws, header_row, _row_key, marks.edited_rows, marks.stale_rows)
    _append_expert_columns(ws, header_row, *_EXPERT_SCORE_COLUMNS)


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


def _render_source_rows_cell(
    for_rows: list[int], row_indicators: dict[int, list[int]], mandatory: list[str] | None = None,
) -> str:
    """
    Обратная ссылка на карту: «стр. N — инд. M», каждая на своей строке клетки.

    Получив от органа цифры, аналитик должен видеть, к какой гипотезе и какому
    индикатору они относятся, не перебирая карту вручную (фикс N 6). Номера
    строк уже посчитаны узлом [6] в RequestItem.for_rows, индикаторы берутся
    из самой карты по row_id.
    """
    # 2.7 (АМ): обязательный показатель по типу акта — первой строкой, с кодом формы
    lines = [f"обязательный ({', '.join(mandatory)})"] if mandatory else []
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
    caption: str, marks: Marks | None = None, act_types: list[str] | None = None,
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
            _render_source_rows_cell(item.for_rows, row_indicators, item.mandatory),
            item.clause or "",
            *year_cells,
        ])
        # подстроки разрезов ссылок не несут — они принадлежат родительской строке
        for i, breakdown in enumerate(item.breakdown, start=1):
            ws.append([f"{item.num}.{i}", breakdown, "", "", *year_cells])

    # открытые вопросы — на своём листе (2.7, Ж2), здесь только цифры и перечни
    counters = [it for it in request.items if it.kind not in (_KIND_LIST, _KIND_QUESTION)]
    lists = [it for it in request.items if it.kind == _KIND_LIST]
    mandatory_counters = [it for it in counters if it.mandatory]
    if mandatory_counters:
        # 2.7 (АМ): сначала обязательные показатели по типу акта, затем — из гипотез
        types = ", ".join(t for t in (act_types or []) if t) or "не указан"
        ws.append(["", f"Обязательные показатели (тип акта: {types})", "", "", *[""] * len(years)])
    for item in mandatory_counters:
        _append_item(item, empty_years)
    other_counters = [it for it in counters if not it.mandatory]
    if mandatory_counters and other_counters:
        ws.append(["", "Показатели из гипотез", "", "", *[""] * len(years)])
    for item in other_counters:
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
    if marks is not None:
        _append_marks_column(ws, header_row, _item_key, marks.edited_items, marks.stale_items)
    _append_expert_columns(ws, header_row, *_EXPERT_SCORE_COLUMNS)


def _render_questions_sheet(
    wb: Workbook, request: DataRequest, hyp_map: HypothesisMap, caption: str, marks: Marks | None = None,
) -> None:
    """
    Лист «Вопросы органу» (2.7, итерация 4, Ж2): № | Вопрос | Пункт акта | Строки карты.

    Владимир (файл 4, правило 3.1): открытые вопросы — «отдельный бланк, если следует из
    гипотез и непонятно из текста акта / открытых данных». Лист создаётся всегда: эксперт
    видит, что вопросов нет, а не гадает, куда делся бланк.
    """
    ws = wb.create_sheet(_SHEET_QUESTIONS)
    _render_caption(ws, caption)
    header_row = ws.max_row + 1
    ws.append(["№", "Вопрос", "Пункт акта", "Строки карты"])
    row_indicators = {row.row_id: row.indicators for row in hyp_map.rows}
    questions = [it for it in request.items if it.kind == _KIND_QUESTION]
    if not questions:
        ws.append(["", "вопросов нет", "", ""])
    for item in questions:
        ws.append([item.num, item.metric, item.clause or "",
                   _render_source_rows_cell(item.for_rows, row_indicators)])
    _format_listing_sheet(ws, {"A": 8, "B": 70, "C": 16, "D": 26}, header_row)
    for row in ws.iter_rows(min_row=header_row + 1, max_col=1):
        row[0].alignment = _CENTER_TOP
    if marks is not None:
        _append_marks_column(ws, header_row, _item_key, marks.edited_items, marks.stale_items)
    _append_expert_columns(ws, header_row, *_EXPERT_SCORE_COLUMNS)


def _indicator_label(v: Verdict) -> str:
    """Номер и имя индикатора в одной клетке — «7. отсутствие избыточных процедур»."""
    return f"{v.indicator}. {v.name}"


def _format_listing_sheet(ws: Worksheet, widths: dict[str, int], header_row: int) -> None:
    """Общее оформление для листов-перечней (Вердикты / Ошибки прогона)."""
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    _style_header_row(ws, header_row)
    for row in ws.iter_rows(min_row=header_row + 1, max_col=len(widths)):
        for c in row:
            c.font = _BODY_FONT
            c.alignment = _WRAP_TOP
            c.border = _BORDER


def _where_details(v: Verdict, hyp_map: HypothesisMap, unverified_ids: dict[int, int]) -> str:
    """
    Колонка «Где подробнее» листа «Вердикты»: куда идти за разбором.

    Нарушен и «нет данных» со строками карты — номера строк карты; «нет данных»
    без строки — номер дописанной строки «Нужна ручная проверка» (тот же, что
    в карте); ошибка — лист ошибок; соблюдён и не применимо — подробностей нет,
    обоснование и цитата уже в этой строке.
    """
    if v.verdict == "ошибка":
        return f"лист «{_SHEET_FAILED}»"
    rows = [row.row_id for row in hyp_map.rows if v.indicator in row.indicators]
    if rows:
        return f"{_SHEET_MAP}, стр. {', '.join(str(r) for r in rows)}"
    if v.indicator in unverified_ids:
        return f"{_SHEET_MAP}, стр. {unverified_ids[v.indicator]} — нужна ручная проверка"
    return "—"


def _render_verdicts_sheet(
    wb: Workbook, buckets: Buckets, hyp_map: HypothesisMap, caption: str,
    unverified_ids: dict[int, int], marks: Marks | None = None,
) -> None:
    """
    Лист «Вердикты»: все вердикты четырёх куч по возрастанию номера индикатора.

    Пул 3 (письмо заказчика 2026-09-04, п. 4): «сейчас одни индикаторы на листе
    „Карта гипотез“, другие — на листе „Проверенные индикаторы“; предлагается
    на втором указать сводную информацию о всех индикаторах (от 1 до 12)».
    Карта остаётся как есть, сюда добавляется колонка «Где подробнее».
    """
    ws = wb.create_sheet(_SHEET_VERDICTS)
    _render_caption(ws, caption)
    header_row = ws.max_row + 1
    ws.append(["Индикатор", "Вердикт", "Обоснование", "На чём основано", "Где подробнее"])
    everything = buckets.violated + buckets.no_data + buckets.checked + buckets.failed
    for v in sorted(everything, key=lambda v: v.indicator):
        # «Обоснование» — текст вердикта, «На чём основано» — цитата и адреса;
        # рендер общий с листом карты, чтобы одно и то же выглядело одинаково
        ws.append([
            _indicator_label(v), v.verdict, _points(v.rationale_brief, v.rationale),
            _render_basis_cell(v.proof, v.clause, list(v.sources)),
            _where_details(v, hyp_map, unverified_ids),
        ])
    _format_listing_sheet(ws, {"A": 34, "B": 16, "C": 60, "D": 52, "E": 30}, header_row)
    # 2.7 (итерация 4, В): «нет данных» — заливкой по всей строке вердикта (без колонок эксперта)
    for row in ws.iter_rows(min_row=header_row + 1, max_col=5):
        if row[1].value == "нет данных":
            for c in row:
                c.fill = _NO_DATA_FILL
    if marks is not None:
        _append_marks_column(ws, header_row, _indicator_key, marks.edited_verdicts, marks.stale_verdicts)
    _append_expert_columns(ws, header_row, *_EXPERT_VERDICT_COLUMNS)


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


def _render_journal_sheet(wb: Workbook, journal: list[JournalEntry]) -> None:
    """Лист «Журнал изменений» — последний; по строке на применённое изменение (план итерации 3, шаг 1.5)."""
    ws = wb.create_sheet(_SHEET_JOURNAL)
    _render_caption(ws, "Изменения после исходного разбора сервиса (версия 0) — по строке на каждое применённое изменение.")
    header_row = ws.max_row + 1
    ws.append(["Версия", "Когда", "Что сделано", "Где", "Было", "Стало", "Почему", "Как", "Стоимость, $"])
    for e in journal:
        ws.append([e.version, e.at, e.action, e.target, e.before or "—", e.after or "—",
                   e.reason or "—", e.kind, round(e.cost_usd, 2)])
    _format_listing_sheet(
        ws, {"A": 9, "B": 18, "C": 36, "D": 26, "E": 40, "F": 40, "G": 40, "H": 18, "I": 12}, header_row,
    )


def build_excel(
    hypothesis_map: HypothesisMap,
    request: DataRequest,
    buckets: Buckets,
    passport: ActPassport | None,
    run_id: str,
    *,
    version: int | None = None,
    journal: list[JournalEntry] | None = None,
    marks: Marks | None = None,
    unverified_ids: dict[int, int] | None = None,
    analyzed_at: datetime.datetime | None = None,
    act_types: list[str] | None = None,
) -> Path:
    """
    Узел [7] — собрать единый .xlsx из готовых карты, запроса и куч вердиктов.

    Модель не используется. Сохраняет книгу как 08_result.xlsx в папке прогона
    и возвращает путь к файлу.

    Годы колонок бланка — из самого бланка: с v2.3 их пишет код узла [6] от даты
    вступления акта в силу (Г5), модель их не выбирает. До 2026-09-25 здесь
    заново считалось окно от сегодняшней даты, и у акта в силе с 2025 года
    в Excel были пустые колонки 2023 и 2024. compute_years — запасной путь для
    бланка без годов.

    version, journal, marks, unverified_ids, analyzed_at — для версий диалогового
    режима (план итерации 3, шаг 1.5); без них книга прежняя. act_types (2.7, АМ) —
    тип акта, выбранный при загрузке: строка на листе «Как заполнять» и подзаголовок
    обязательных показателей на листе запроса.
    """
    logger = get_run_logger(run_id).getChild("08_excel")

    years = request.years or compute_years(num_years=REQUEST_YEARS_DEPTH)
    unverified = _unverified_row_ids(hypothesis_map, buckets, unverified_ids)
    row_to_nums = _row_to_request_nums(request)
    row_to_question_nums = _row_to_request_nums(request, questions=True)

    # Числитель подписей считается по факту прогона, знаменатель — из конфига:
    # 12 индикаторов задаёт методика, зашивать это число в текст нельзя.
    total = len(all_indicators())
    map_indicators = {n for row in hypothesis_map.rows for n in row.indicators}
    map_indicators |= {v.indicator for v in _unverified_verdicts(hypothesis_map, buckets)}

    wb = Workbook()
    _render_howto_sheet(wb, passport, analyzed_at, version, act_types)   # первый лист — инструкция эксперту
    _render_map_sheet(
        wb, hypothesis_map, row_to_nums, passport, buckets,
        f"Индикаторы, по которым найдено нарушение или не хватило данных — "
        f"{len(map_indicators)} из {total}. По каждому построена гипотеза: "
        f"что не так, чем мерить эффект, какие данные нужны — либо отмечено, "
        f"что нужна ручная проверка.",
        unverified, marks, row_to_question_nums,
    )
    # 2.7 (А): бланк адресован разработчику — называем его, если он определён
    developers = passport.requisites.developers if passport is not None else []
    request_caption = (
        "Заполняет: " + "; ".join(f"{d.name}" + (f" ({d.scope})" if d.scope and d.scope != "весь акт" else "")
                                  for d in developers)
        + ". Данные, которых нет в открытом доступе, три последних завершённых года."
        if developers else
        "Данные, которых нет в открытом доступе. Заполняет орган-разработчик, "
        "три последних завершённых года."
    )
    _render_request_sheet(
        wb, request, years, hypothesis_map, request_caption, marks, act_types,
    )
    _render_questions_sheet(
        wb, request, hypothesis_map,
        "Вопросы органу словами — то, что нельзя запросить числом или перечнем. Каждый вопрос "
        "вытекает из гипотезы карты (строка) и пункта акта.",
        marks,
    )
    n_verdicts = sum(len(b) for b in (buckets.violated, buckets.no_data, buckets.checked, buckets.failed))
    _render_verdicts_sheet(
        wb, buckets, hypothesis_map,
        f"Все индикаторы с вердиктами — {n_verdicts} из {total}. По нарушенным и «нет данных» "
        f"подробности на листе «{_SHEET_MAP}», по сбоям — на листе «{_SHEET_FAILED}». "
        f"Подсвечены индикаторы, по которым сведений для вердикта не нашлось ни в Гаранте, "
        f"ни в интернете. Если знаете, где они лежат, — напишите в графе «Где искать».",
        unverified, marks,
    )
    _render_failed_sheet(
        wb, buckets.failed + _violated_without_hypotheses(hypothesis_map, buckets),
        f"Индикаторы, проверка которых не завершилась. "
        f"Пусто — значит все {total} отработали.",
    )
    if journal is not None:
        _render_journal_sheet(wb, journal)

    out_path = run_dir(run_id) / "08_result.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)  # папка прогона может ещё не существовать
    wb.save(out_path)

    logger.info(
        "excel: rows=%d request_items=%d checked=%d failed=%d path=%s",
        len(hypothesis_map.rows), len(request.items), len(buckets.checked),
        len(buckets.failed), out_path,
    )
    return out_path
