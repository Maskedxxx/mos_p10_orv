"""
Узел [6] пайплайна П-10 — сборка запроса показателей (app/nodes/request.py).

Один вызов модели на всю карту гипотез, без параллельного прогона (сжатие
похожих формулировок в один пункт запроса требует видеть все строки карты
сразу, как и слияние дублей в узле [5]). Инструменты не нужны — модель
работает только с уже готовой картой, в интернет ходить незачем.

В stdin уходит карта гипотез (список строк карты) в читаемом JSON, отдельным
помеченным блоком (_build_document). Текст акта тут не нужен — карта уже
содержит всё необходимое (гипотеза, эффект, показатели с пометкой источника),
см. prompts/06_request.md.

Провал вызова НЕ выдумывает запрос — возвращается DataRequest с пустым
списком items и годами по умолчанию (compute_years(REQUEST_YEARS_DEPTH),
переиспользуется из app.nodes.excel — то же вычисление, что определяет
колонки-годы итогового листа запроса), предупреждение пишется в лог. Excel
соберётся и в этом случае, лист запроса будет просто пустым.

После успешного ответа модели — две проверки, обе пишут в лог, ни одна не
роняет узел:

1. Каждый номер строки в RequestItem.for_rows обязан существовать среди
   row_id карты — иначе показатель ссылается в никуда (_check_for_rows_exist,
   уровень ERROR).
2. В запрос не должно попасть ничего с источником «открытый» — модель обязана
   отбирать только показатели с пометкой «запрос органу». Точная сверка
   возможна только на уровне СТРОКИ карты: контракт RequestItem.for_rows
   ссылается на MapRow.row_id, а не на конкретный DataItem внутри строки,
   детальнее сверить нечем. Если показатель запроса ссылается только на
   строки, где НИ ОДНОГО показателя с пометкой «запрос органу» нет вообще —
   это явный сигнал, что модель притащила лишнее (_check_no_open_source_leaked,
   уровень WARNING — предупреждение, не ошибка, по требованию владельца).

Стоимость вызова ПРИБАВЛЯЕТСЯ к уже записанной в meta.json — переиспользуется
_add_cost_to_meta из app.nodes.hypotheses, вторая копия не пишется.
"""

from __future__ import annotations

import json

from app.config import ALLOWED_TOOLS_TEXT, PROMPT_REQUEST, REQUEST_YEARS_DEPTH
from app.contracts import DataRequest, HypothesisMap, RequestItem, RequestOutput
from app.mandatory_indicators import mandatory_for
from app.logger import get_run_logger
from app.nodes.excel import compute_years
from app.nodes.hypotheses import _add_cost_to_meta
from app.runner import call
from app.trace import save_json

# Заголовок блока текущего бланка в режиме дополнения (план итерации 3, шаг 1.4).
# Этот же текст описан в промпте 06_request.md — сверяет tests/test_prompts.py.
EXISTING_BLANK_HEADER = "## УЖЕ В БЛАНКЕ (JSON)"


def _request_years(effective_year: int | None, end_year: int | None = None) -> list[int]:
    """
    Годы колонок бланка: окно compute_years, обрезанное снизу годом вступления
    акта в силу (ОС экспертов 2026-09-04, узел 6, Г5).

    За годы до вступления в силу у органа данных нет и быть не может: у акта
    с 21.08.2025 колонки 2023 и 2024 пусты по определению, и модель пыталась
    выразить «с даты вступления» разрезом «по подпериодам» — эксперт: «про какие
    подпериоды речь?». Если окно обрезалось целиком (акт вступил позже конца
    окна), остаётся один год вступления — на решение владельца.
    """
    base = compute_years(num_years=REQUEST_YEARS_DEPTH, end_year=end_year)
    if effective_year is None:
        return base
    clipped = [y for y in base if y >= effective_year]
    return clipped or [effective_year]


def _build_document(
    hypothesis_map: HypothesisMap,
    years: list[int],
    document_md: str | None = None,
    passport: dict | None = None,
    existing: DataRequest | None = None,
    mandatory: list[dict] | None = None,
) -> str:
    """
    Собрать документ для stdin: текст акта, паспорт, карта гипотез, годы бланка —
    четырьмя помеченными блоками (ОС экспертов 2026-09-04, узел 6, Г5/Г6).

    Текст акта нужен, чтобы раскрывать разрезы «по основаниям п. X» (эксперты:
    «основания надо раскрыть») и проверять привязку к пункту; паспорт — адресат
    бланка и статус акта; годы — посчитаны кодом, модель их переписывает.
    Блоки акта и паспорта опускаются, если не переданы (совместимость с вызовом
    из пайплайна до внедрения паспорта). Merges карты не нужны.
    """
    rows_json = json.dumps(
        [row.model_dump(mode="json") for row in hypothesis_map.rows],
        ensure_ascii=False, indent=2,
    )
    parts: list[str] = []
    if document_md is not None:
        parts.append("## ПОЛНЫЙ ТЕКСТ АКТА\n\n" f"{document_md}\n")
    if passport is not None:
        parts.append(
            "## ПАСПОРТ АКТА\n\n"
            f"```json\n{json.dumps(passport, ensure_ascii=False, indent=2)}\n```\n"
        )
    if existing is not None:
        # Режим дополнения: модель видит уже составленный бланк, чтобы не повторять
        # его позиции; в карте при этом только новые строки
        blank = [
            {"num": it.num, "kind": it.kind, "metric": it.metric, "unit": it.unit, "clause": it.clause}
            for it in existing.items
        ]
        parts.append(f"{EXISTING_BLANK_HEADER}\n\n```json\n{json.dumps(blank, ensure_ascii=False, indent=2)}\n```\n")
    parts.append("## КАРТА ГИПОТЕЗ (JSON)\n\n" f"```json\n{rows_json}\n```\n")
    if mandatory:
        # 2.7 (АМ): обязательные показатели выбранных типов акта — дословно из формы Владимира
        parts.append(f"{MANDATORY_HEADER}\n\n```json\n{json.dumps(mandatory, ensure_ascii=False, indent=2)}\n```\n")
    parts.append("## ГОДЫ БЛАНКА (JSON)\n\n" f"```json\n{json.dumps(years)}\n```\n")
    return "\n".join(parts)


MANDATORY_HEADER = "## ОБЯЗАТЕЛЬНЫЕ ПОКАЗАТЕЛИ (JSON)"
_KIND_RANK = {"показатель по годам": 0, "перечень или реестр": 1, "открытый вопрос": 2}


def _mandatory_item(entry: dict) -> RequestItem:
    """Обязательный показатель, который модель не включила, — позицией дословно из формы (Р4)."""
    note = f"Разбивка из формы: {entry['breakdown']}" if entry["breakdown"] else None
    return RequestItem(num="0", kind=entry["kind"], metric=entry["metric"], unit=entry["unit"],
                       note=note, mandatory=list(entry["codes"]))


def _ensure_mandatory(request: DataRequest, mandatory: list[dict], logger) -> DataRequest:
    """
    Каждый обязательный код — хотя бы в одной позиции. Пропущенный моделью показатель
    код дописывает дословно (решение владельца Р4, 12.10: «всегда добавляются» иначе
    не гарантировать) и пишет предупреждение.
    """
    covered = {c for it in request.items for c in it.mandatory}
    added = [_mandatory_item(e) for e in mandatory if not set(e["codes"]) & covered]
    for it in added:
        logger.warning("request: обязательный показатель %s не вошёл в ответ модели — дописан кодом дословно",
                       ", ".join(it.mandatory))
    return request.model_copy(update={"items": request.items + added}) if added else request


def _order_and_number(request: DataRequest) -> DataRequest:
    """Порядок бланка: по родам (показатели → перечни → вопросы), внутри рода сначала обязательные; номера подряд."""
    ordered = sorted(request.items, key=lambda it: (_KIND_RANK[it.kind], 0 if it.mandatory else 1))
    items = [it.model_copy(update={"num": str(i)}) for i, it in enumerate(ordered, start=1)]
    return request.model_copy(update={"items": items})


# Источники показателей карты, которые уходят органу: цифры и (2.7, Ж2) вопросы словами
_TO_AUTHORITY = ("запрос органу", "вопрос органу")


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _check_coverage(hyp_map: HypothesisMap, request: DataRequest, logger) -> None:
    """
    Проверка 4 (Г4): каждый показатель карты с источником «запрос органу» вошёл
    ровно в одну позицию бланка либо назван в why_dropped.

    Аналог _check_no_loss узла [5]. Показатель в нескольких позициях — WARNING,
    не ERROR: разделение «сумма и количество» на две позиции законно (Г1).
    Ответ по промпту v1 (без sources) проверить нельзя — тогда WARNING и выход,
    чтобы не сыпать ложные ERROR.
    """
    if not any(item.sources for item in request.items):
        logger.warning("request: покрытие не проверить — модель не вернула sources (промпт v1?)")
        return
    internal = {
        (row.row_id, _norm(dp.metric)): dp.metric
        for row in hyp_map.rows for dp in row.data if dp.source in _TO_AUTHORITY
    }
    where: dict[tuple[int, str], list[str]] = {}
    for item in request.items:
        for src in item.sources:
            where.setdefault((src.row_id, _norm(src.metric)), []).append(item.num)
    for key, nums in where.items():
        if len(nums) > 1:
            # Либо законное разделение показателя с двумя величинами («сумма и
            # количество» → две позиции), либо дубль — решает аналитик, поэтому WARNING
            logger.warning(
                "request: показатель карты «%s» (строка %d) вошёл в несколько позиций (%s) — "
                "разделение двух величин или дубль, проверь",
                key[1][:80], key[0], ", ".join(nums),
            )
    dropped = _norm(request.why_dropped)
    for key, metric in internal.items():
        if key in where:
            continue
        if _norm(metric) in dropped:
            logger.info("request: показатель «%s» (строка %d) не взят с объяснением", metric[:80], key[0])
        else:
            logger.error(
                "request: показатель «%s» (строка %d) не вошёл ни в одну позицию и не объяснён в why_dropped",
                metric[:80], key[0],
            )


def _valid_row_ids(hyp_map: HypothesisMap) -> set[int]:
    """Множество существующих row_id карты — для проверки ссылок for_rows."""
    return {row.row_id for row in hyp_map.rows}


def _rows_with_internal_data(hyp_map: HypothesisMap) -> set[int]:
    """
    row_id строк карты, где есть хотя бы один показатель с источником
    «запрос органу». Строка без единого такого показателя не должна
    порождать пункт запроса (см. docstring модуля, проверка 2).
    """
    return {
        row.row_id for row in hyp_map.rows
        if any(dp.source in _TO_AUTHORITY for dp in row.data)
    }


def _check_for_rows_exist(request: DataRequest, valid_ids: set[int], logger) -> None:
    """Проверка 1: каждый номер в for_rows должен существовать среди row_id карты."""
    for item in request.items:
        for row_id in item.for_rows:
            if row_id not in valid_ids:
                logger.error(
                    "request: показатель № %s ссылается на несуществующую строку "
                    "карты %d",
                    item.num, row_id,
                )


def _check_no_open_source_leaked(
    request: DataRequest, internal_rows: set[int], logger,
) -> None:
    """Проверка 2: в запрос не должно попасть ничего с источником «открытый» (см. docstring)."""
    for item in request.items:
        if item.for_rows and not any(row_id in internal_rows for row_id in item.for_rows):
            logger.warning(
                "request: показатель № %s ссылается только на строки без "
                "показателей с источником «запрос органу» — похоже, модель "
                "притащила лишнее",
                item.num,
            )


def _append(existing: DataRequest, new: DataRequest) -> DataRequest:
    """Дописать новые позиции после последнего номера бланка; существующие не трогаются."""
    start = max((int(it.num) for it in existing.items), default=0) + 1
    renumbered = [it.model_copy(update={"num": str(start + i)}) for i, it in enumerate(new.items)]
    why_dropped = "\n".join(s for s in (existing.why_dropped, new.why_dropped) if s)
    return DataRequest(items=existing.items + renumbered, years=existing.years, why_dropped=why_dropped)


def build_request(
    hypothesis_map: HypothesisMap,
    run_id: str,
    *,
    document_md: str | None = None,
    passport: dict | None = None,
    effective_year: int | None = None,
    timeout_s: int = 900,
    max_budget_usd: float | None = None,
    existing: DataRequest | None = None,
    trace_prefix: str = "",
    save: bool = True,
    act_types: list[str] | None = None,
) -> DataRequest:
    """
    Узел [6] — собрать запрос показателей одним вызовом модели по готовой карте.

    Режим дополнения (диалог, план итерации 3, шаг 1.4): existing — текущий бланк,
    hypothesis_map — ТОЛЬКО новые строки карты. Модель составляет позиции под них,
    код нумерует их после последнего номера бланка, годы берутся из бланка,
    существующие позиции не меняются; при сбое модели бланк возвращается как был.
    trace_prefix и save — как у узлов вердиктов и гипотез: своя папка сырых ответов,
    кандидат без записи 07_request.json.

    Провал вызова возвращает пустой DataRequest с годами по умолчанию (см.
    docstring модуля), успешный ответ проходит две проверки-предупреждения,
    ни одна из них не роняет узел. Артефакты: сырой ответ сохраняет runner.call
    под именем 07_request.raw.json, готовый запрос — здесь под именем
    07_request.json. Стоимость прибавляется к уже записанной в meta.json.

    act_types (2.7, АМ) — типы акта, выбранные при загрузке: их обязательные показатели
    уходят модели отдельным блоком и гарантированно попадают в бланк (пропущенные
    дописывает код). Только для нового бланка — в режиме дополнения не используются.
    """
    logger = get_run_logger(run_id).getChild("07_request")
    # Годы дополняемого бланка не пересчитываются от сегодняшней даты — они уже в нём
    years = existing.years if existing is not None else _request_years(effective_year)
    mandatory = mandatory_for(act_types or []) if existing is None else []

    if not hypothesis_map.rows and mandatory:
        # Карта пуста, но тип акта выбран — бланк из одних обязательных показателей, модель не нужна
        logger.info("request: строк карты нет — бланк из %d обязательных показателей", len(mandatory))
        value = _order_and_number(_ensure_mandatory(DataRequest(items=[], years=years), mandatory, logger))
        if save:
            save_json(run_id, "07_request.json", value)
        return value

    if not hypothesis_map.rows:
        # Карта пуста (все индикаторы соблюдены/не применимы) либо новых строк нет —
        # запрашивать нечего, вызывать модель незачем, это честный результат.
        logger.info("request: строк карты нет, вызов модели пропущен")
        value = existing if existing is not None else DataRequest(items=[], years=years)
        if save:
            save_json(run_id, "07_request.json", value)
        return value

    instruction = PROMPT_REQUEST.read_text(encoding="utf-8")
    document = _build_document(hypothesis_map, years, document_md, passport, existing, mandatory)

    result = call(
        instruction,
        document,
        RequestOutput,   # строгая нумерация — в ответе модели (контракт, шаг 1.3)
        run_id=run_id,
        trace_name=f"{trace_prefix}07_request.raw.json",
        allowed_tools=ALLOWED_TOOLS_TEXT,
        timeout_s=timeout_s,
        max_budget_usd=max_budget_usd,
    )

    if not result.ok or result.value is None:
        logger.warning(
            "request: вызов модели провалился (%s) — %s",
            result.error, "бланк без изменений" if existing is not None else "пустой запрос, годы по умолчанию",
        )
        value = existing if existing is not None else DataRequest(items=[], years=years)
        if mandatory:
            value = _order_and_number(_ensure_mandatory(value, mandatory, logger))
    else:
        # Годы — только от кода: модель их переписывает из входа, но решает не она
        value = result.value.model_copy(update={"years": years})
        _check_for_rows_exist(value, _valid_row_ids(hypothesis_map), logger)
        _check_no_open_source_leaked(value, _rows_with_internal_data(hypothesis_map), logger)
        _check_coverage(hypothesis_map, value, logger)
        if existing is not None:
            value = _append(existing, value)
        elif mandatory:
            value = _order_and_number(_ensure_mandatory(value, mandatory, logger))

    if save:
        save_json(run_id, "07_request.json", value)
    _add_cost_to_meta(run_id, result.cost_usd)

    logger.info(
        "request done: items=%d years=%s cost=$%.4f",
        len(value.items), value.years, result.cost_usd,
    )
    return value
