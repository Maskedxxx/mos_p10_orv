"""Сборка карты одним вызовом модели из всех исходных гипотез.

Кандидаты по совпадению номеров пунктов — только подсказка. Решения вне
списка допустимы. Пропущенный кандидат фиксируется в журнале как ERROR;
сам по себе этот пропуск не отклоняет ответ.

Контекстная проверка свежего MergeOutput проверяет исходные ID, сохранность
неслитых строк, расчётов и данных. Ошибка вызывает существующее ограниченное
исправление ответа. При исчерпании попыток карта собирается из входа без
слияний; сырые неудачные ответы сохраняет runner. Юридическое тождество
механизмов проверка Python не устанавливает.

Частичные пересечения сохраняются в decisions.overlap/accounting_note и
журнале. Формулы и суммы автоматически не исправляются, Excel-предупреждения
отдельно не добавляются.
"""

from __future__ import annotations

from app.input_contexts import merge_input

import json
import re

from app.config import ALLOWED_TOOLS_TEXT, PROMPT_MERGE
from app.contracts import Hypothesis, HypothesisMap, HypothesisSet, MapRow, MergeOutput
from app.formatting import format_cost
from app.logger import get_run_logger
from app.runner import call
from app.trace import MAP_FILE, add_cost, save_json


def flatten_hypotheses(hypothesis_sets: list[HypothesisSet]) -> list[tuple[int, int, Hypothesis]]:
    """
    Разложить все входящие гипотезы в плоский список со сквозным номером с 1.

    Порядок — по порядку hypothesis_sets (в hypotheses.py они уже отсортированы
    по номеру индикатора), внутри набора — по порядку hypotheses. Возвращает
    список (row_id, indicator, Hypothesis); row_id — тот самый номер, на который
    модель будет ссылаться в merged_row_ids.
    """
    flat: list[tuple[int, int, Hypothesis]] = []
    row_id = 1
    for hset in hypothesis_sets:
        for hyp in hset.hypotheses:
            flat.append((row_id, hset.indicator, hyp))
            row_id += 1
    return flat


# Номер пункта акта: «2.7», «5.3.6», «3.4.2(1)», «8». Голые числа без точки берём
# только короткие (до двух цифр) — это пункты вида «п. 8», а не годы и номера актов.
_CLAUSE_TOKEN_RE = re.compile(r"\d+(?:\.\d+)*(?:\(\d+\))?")
# «приложение 1», «постановление № 743-ПП» внутри clause — не пункты, а адрес;
# вырезаются до разбора, иначе все гипотезы одного приложения станут кандидатами.
_CLAUSE_NOISE_RE = re.compile(
    r"[Пп]риложени\w*\s*(?:№\s*)?\d+|[Пп]остановлени\w*\s*№?\s*\d+[-А-Яа-я]*"
)


def clause_tokens(clause: str | None) -> set[str]:
    """Номера пунктов акта из строки clause гипотезы; пусто, если пункта нет."""
    if not clause:
        return set()
    cleaned = _CLAUSE_NOISE_RE.sub(" ", clause)
    return {t for t in _CLAUSE_TOKEN_RE.findall(cleaned) if "." in t or len(t) <= 2}


def _candidate_pairs(flat: list[tuple[int, int, Hypothesis]]) -> list[tuple[int, int, list[str]]]:
    """
    Пары гипотез с общим номером пункта акта — кандидаты на слияние (ОС экспертов,
    узел 5, В2/В4).

    Совпавший номер — только подсказка: он не доказывает дубль и не ограничивает
    проверку моделью остальных пар. Кандидаты могут относиться к разным приложениям.
    Возвращает (row_id_a, row_id_b, общие пункты) в порядке номеров.
    """
    tokens = [(row_id, clause_tokens(hyp.clause)) for row_id, _indicator, hyp in flat]
    pairs: list[tuple[int, int, list[str]]] = []
    for i in range(len(tokens)):
        for j in range(i + 1, len(tokens)):
            shared = tokens[i][1] & tokens[j][1]
            if shared:
                pairs.append((tokens[i][0], tokens[j][0], sorted(shared)))
    return pairs


def _build_document(
    document_md: str,
    flat: list[tuple[int, int, Hypothesis]],
    candidates: list[tuple[int, int, list[str]]],
) -> str:
    """Все исходные гипотезы и пары кандидатов; текст акта здесь не передаётся.

    document_md остаётся аргументом для совместимости вызывающего кода.
    """
    packet = merge_input(flat, candidates).model_dump(mode="json")
    hyps_json = json.dumps(packet["hypotheses"], ensure_ascii=False, indent=2)
    cands_json = json.dumps(packet["candidates"], ensure_ascii=False, indent=2)
    return (
        "## ПРОНУМЕРОВАННЫЕ ГИПОТЕЗЫ (JSON)\n\n"
        f"```json\n{hyps_json}\n```\n\n"
        "## КАНДИДАТЫ НА СЛИЯНИЕ (JSON)\n\n"
        f"```json\n{cands_json}\n```\n"
    )


def _check_decisions(
    candidates: list[tuple[int, int, list[str]]], hyp_map: HypothesisMap, logger,
) -> None:
    """
    Проверить, что по каждому кандидату модель дала решение.

    Кандидат без решения — тот самый «пропуск на глаз», ради которого кандидаты
    введены. Как и потеря строки (_check_no_loss), фиксируется в логе как ERROR:
    карта при этом сохраняется — слияние улучшает читаемость, но не должно ронять
    прогон.
    """
    decided = {frozenset(d.row_ids) for d in hyp_map.decisions}
    for a, b, shared in candidates:
        if frozenset((a, b)) not in decided:
            logger.error(
                "merge: кандидат без решения %s (общий пункт %s) — модель обошла пару молча",
                [a, b], ", ".join(shared),
            )


def fallback_map(flat: list[tuple[int, int, Hypothesis]]) -> HypothesisMap:
    """
    Провал модели — не выдумываем слияние: каждая входящая гипотеза становится
    отдельной строкой карты как есть, merges остаётся пустым (см. docstring
    модуля). Гипотезы теряться не должны никогда, слияние — не обязательное
    улучшение.
    """
    rows = [
        MapRow(
            row_id=row_id,
            indicators=[indicator],
            hypothesis=hyp.hypothesis,
            grounds=hyp.grounds,
            effects=hyp.effects,
            data=hyp.data,
            proof=hyp.proof,
            clause=hyp.clause,
            hypothesis_brief=hyp.hypothesis_brief,   # 2.5: выжимки для эксперта — как у гипотезы
            grounds_brief=hyp.grounds_brief,
        )
        for row_id, indicator, hyp in flat
    ]
    return HypothesisMap(rows=rows, merges=[])


def _check_no_loss(n_input: int, hyp_map: HypothesisMap, logger) -> None:
    """
    Проверить, что ни одна входящая гипотеза не пропала при слиянии.

    Каждое слияние k исходных строк в одну законно "поглощает" (k - 1) строк.
    Если строк на выходе плюс суммарно поглощённое не равно числу гипотез на
    входе — модель где-то молча выбросила строку без объявления слияния.
    Это дополнительная диагностика; точная проверка входных ID и полей
    выполняется контрактом свежего ответа через validation_context.
    """
    n_output = len(hyp_map.rows)
    total_merged_ids = sum(len(m.merged_row_ids) for m in hyp_map.merges)
    absorbed = total_merged_ids - len(hyp_map.merges)
    if n_output + absorbed != n_input:
        logger.error(
            "merge: расхождение в счёте строк — вход=%d выход=%d слияний=%d "
            "поглощено=%d (ожидалось выход+поглощено=вход, похоже модель "
            "выбросила строку молча)",
            n_input, n_output, len(hyp_map.merges), absorbed,
        )


def merge_map(
    hypothesis_sets: list[HypothesisSet],
    document_md: str,
    run_id: str,
    *,
    timeout_s: int = 900,
    max_budget_usd: float | None = None,
) -> HypothesisMap:
    """
    Узел [5] — собрать итоговую карту гипотез одним вызовом модели.

    Слияние дублей видно только когда все гипотезы лежат рядом, поэтому вызов
    ровно один на весь набор, без параллельного прогона по кускам. Провал
    вызова не выдумывает карту (см. fallback_map), успешный ответ проверяется
    на сохранность гипотез (см. _check_no_loss).

    Артефакты: сырой ответ модели сохраняет сам runner.call под именем
    06_map.raw.json, готовая карта сохраняется здесь под именем 06_map.json.
    Стоимость вызова прибавляется к уже записанной в meta.json.
    """
    logger = get_run_logger(run_id).getChild("06_merge")

    flat = flatten_hypotheses(hypothesis_sets)
    n_input = len(flat)

    if n_input == 0:
        # Нечего сливать — вызывать модель не за чем, это честный пустой результат
        # (все индикаторы соблюдены/не применимы), а не сбой.
        logger.info("merge: входных гипотез нет, вызов модели пропущен")
        empty = HypothesisMap(rows=[], merges=[])
        save_json(run_id, MAP_FILE, empty)
        return empty

    instruction = PROMPT_MERGE.read_text(encoding="utf-8")
    candidates = _candidate_pairs(flat)
    document = _build_document(document_md, flat, candidates)

    result = call(
        instruction,
        document,
        MergeOutput,   # строгая нумерация — в ответе модели (контракт, шаг 1.3)
        run_id=run_id,
        trace_name="06_map.raw.json",
        allowed_tools=ALLOWED_TOOLS_TEXT,
        timeout_s=timeout_s,
        max_budget_usd=max_budget_usd,
        validation_context={"merge_inputs": merge_input(flat, candidates).model_dump(mode="json")["hypotheses"]},
    )

    if not result.ok or result.value is None:
        logger.warning(
            "merge: вызов модели провалился (%s) — карта собрана из входа без "
            "слияний, гипотезы не потеряны",
            result.error,
        )
        hyp_map = fallback_map(flat)
    else:
        hyp_map = result.value
        _check_no_loss(n_input, hyp_map, logger)
        _check_decisions(candidates, hyp_map, logger)
        for decision in hyp_map.decisions:
            if decision.overlap:
                logger.warning("merge: частичное пересечение исходных строк %s: %s. Условие учёта: %s",
                               decision.row_ids, decision.overlap, decision.accounting_note)

    save_json(run_id, MAP_FILE, hyp_map)
    add_cost(run_id, result.cost_usd)

    logger.info(
        "merge done: input=%d rows=%d merges=%d candidates=%d decisions=%d cost=%s",
        n_input, len(hyp_map.rows), len(hyp_map.merges), len(candidates),
        len(hyp_map.decisions), format_cost(result.cost_usd, result.cost_known, digits=4),
    )
    return hyp_map
