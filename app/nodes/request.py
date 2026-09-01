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
from app.contracts import DataRequest, HypothesisMap
from app.logger import get_run_logger
from app.nodes.excel import compute_years
from app.nodes.hypotheses import _add_cost_to_meta
from app.runner import call
from app.trace import save_json


def _build_document(hypothesis_map: HypothesisMap) -> str:
    """
    Собрать документ для stdin: строки карты гипотез в читаемом JSON, отдельным
    помеченным блоком. Merges сюда не нужны — узлу [6] важны только сами
    строки карты (номер, гипотеза, эффект, показатели с источником).
    """
    rows_json = json.dumps(
        [row.model_dump(mode="json") for row in hypothesis_map.rows],
        ensure_ascii=False, indent=2,
    )
    return "## КАРТА ГИПОТЕЗ (JSON)\n\n" f"```json\n{rows_json}\n```\n"


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
        if any(dp.source == "запрос органу" for dp in row.data)
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


def build_request(
    hypothesis_map: HypothesisMap,
    run_id: str,
    *,
    timeout_s: int = 900,
    max_budget_usd: float | None = None,
) -> DataRequest:
    """
    Узел [6] — собрать запрос показателей одним вызовом модели по готовой карте.

    Провал вызова возвращает пустой DataRequest с годами по умолчанию (см.
    docstring модуля), успешный ответ проходит две проверки-предупреждения,
    ни одна из них не роняет узел. Артефакты: сырой ответ сохраняет runner.call
    под именем 07_request.raw.json, готовый запрос — здесь под именем
    07_request.json. Стоимость прибавляется к уже записанной в meta.json.
    """
    logger = get_run_logger(run_id).getChild("07_request")

    if not hypothesis_map.rows:
        # Карта пуста (все индикаторы соблюдены/не применимы) — запрашивать
        # нечего, вызывать модель незачем, это честный пустой результат.
        logger.info("request: карта гипотез пуста, вызов модели пропущен")
        value = DataRequest(items=[], years=compute_years(num_years=REQUEST_YEARS_DEPTH))
        save_json(run_id, "07_request.json", value)
        return value

    instruction = PROMPT_REQUEST.read_text(encoding="utf-8")
    document = _build_document(hypothesis_map)

    result = call(
        instruction,
        document,
        DataRequest,
        run_id=run_id,
        trace_name="07_request.raw.json",
        allowed_tools=ALLOWED_TOOLS_TEXT,
        timeout_s=timeout_s,
        max_budget_usd=max_budget_usd,
    )

    if not result.ok or result.value is None:
        logger.warning(
            "request: вызов модели провалился (%s) — пустой запрос, годы по умолчанию",
            result.error,
        )
        value = DataRequest(items=[], years=compute_years(num_years=REQUEST_YEARS_DEPTH))
    else:
        value = result.value
        _check_for_rows_exist(value, _valid_row_ids(hypothesis_map), logger)
        _check_no_open_source_leaked(value, _rows_with_internal_data(hypothesis_map), logger)

    save_json(run_id, "07_request.json", value)
    _add_cost_to_meta(run_id, result.cost_usd)

    logger.info(
        "request done: items=%d years=%s cost=$%.4f",
        len(value.items), value.years, result.cost_usd,
    )
    return value
