"""
Узел [2] пайплайна П-10 — параллельный прогон двенадцати агентов-индикаторов с барьером.

Каждый индикатор эффективного регулирования проверяется отдельным вызовом модели
(runner.call) в своём потоке — concurrent.futures.ThreadPoolExecutor. Барьер:
управление возвращается только после того, как отработали ВСЕ запрошенные
индикаторы — выход из блока `with ThreadPoolExecutor(...)` дожидается завершения
всех задач (shutdown(wait=True)).

Провал агента (ok=False у CallResult) или необработанное исключение внутри потока
НИКОГДА не превращается в "соблюдён" — только в явный вердикт "ошибка". Молча
проглоченный сбой дал бы приказу зелёный свет там, где его просто не проверяли —
это прямое требование владельца.

В модель уходят полный текст акта под заголовком «## ПОЛНЫЙ ТЕКСТ АКТА» и паспорт
акта под заголовком «## ПАСПОРТ АКТА» (JSON) — ровно так описаны входные данные
в промптах индикаторов v2. Пруф обязан быть дословной цитатой из акта, поэтому
полный текст нужен в любом случае; паспорт говорит агенту, ГДЕ искать и через
какие ключи открываются ворота «не применимо».

Инструменты — config.indicator_tools(n): встроенные по маршруту ("текст" — пусто,
"текст+интернет" — WebSearch + WebFetch, индикаторам 1, 2, 5 — ещё WebFetch) и (2.7)
инструменты Гаранта по таблице INDICATOR_GARANT_TOOLS.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.config import (
    CACHE_WARMUP_S,
    COMPARISON_REGIONS,
    INDICATOR_TIMEOUT_S,
    SEARCH_LIMITS,
    all_indicators,
    get_indicator,
    indicator_tools,
    prompt_path,
)
from app.contracts import ActPassport, Indicator01Output, Indicator02Output, Indicator03Output, Verdict, VerdictOutput
from app.logger import get_run_logger
from app.runner import call
from app.nodes.hypotheses import _add_cost_to_meta, _after, append_task_clarification
from app.trace import save_json

# Длина, до которой обрезается текст ошибки в rationale вердикта "ошибка"
_ERROR_RATIONALE_LIMIT = 2000

def _build_document(document_md: str, passport_block_text: str) -> str:
    """
    Собрать документ для stdin агента: полный текст акта, затем блок паспорта
    (пустая строка, если паспорта нет — так вызывают тесты узла).
    """
    document = "## ПОЛНЫЙ ТЕКСТ АКТА\n\n" + document_md
    if passport_block_text:
        document += "\n\n" + passport_block_text
    return document


# Метки в промптах индикаторов, которые сравнивают Москву с другими регионами (2.7,
# итерация 4, доработка К): перечень регионов не вписан в текст трёх промптов, а
# подставляется здесь — из настроек прогона, по умолчанию COMPARISON_REGIONS конфига.
_REGIONS_LABEL = "{{РЕГИОНЫ_СРАВНЕНИЯ}}"
_REGIONS_COUNT_LABEL = "{{ЧИСЛО_РЕГИОНОВ}}"
# Потолок заходов поиска в интернете (Л): из настроек прогона, по умолчанию SEARCH_LIMITS
_LIMIT_LABEL = "{{ПОТОЛОК_ЗАХОДОВ}}"


def fill_prompt_labels(prompt_text: str, regions: list[str] | None = None, limit: int | None = None) -> str:
    """Подставить в текст промпта регионы сравнения, их число (цифрами) и потолок заходов поиска."""
    regions = list(regions) if regions else list(COMPARISON_REGIONS)
    text = (prompt_text
            .replace(_REGIONS_LABEL, ", ".join(regions))
            .replace(_REGIONS_COUNT_LABEL, str(len(regions))))
    return text.replace(_LIMIT_LABEL, str(limit)) if limit is not None else text


# Свой контракт индикатора (13.10, разбор узлов): артефакт проверки и рубрика вердикта — в описаниях
# полей, поля артефакта стоят перед разбором и вердиктом. Остальные индикаторы — общий VerdictOutput.
INDICATOR_OUTPUT: dict[int, type] = {1: Indicator01Output, 2: Indicator02Output, 3: Indicator03Output}


def _split_artifacts(value: Any) -> tuple[Verdict, dict[str, Any]]:
    """
    Ответ агента → вердикт прежнего вида (его читают гипотезы, Excel, чат) и поля артефакта,
    которых в Verdict нет (сохраняются рядом, в NN.checks.json). Общий контракт — артефактов нет.
    """
    extra = [f for f in type(value).model_fields if f not in Verdict.model_fields]
    if not extra:
        return value, {}
    data = value.model_dump(mode="json")
    artifacts = {f: data.pop(f) for f in extra}
    return Verdict(**data), artifacts


def _error_verdict(indicator_number: int, name: str, error_text: str, attempts: int) -> Verdict:
    """Собрать вердикт "ошибка" вручную — провал агента никогда не становится "соблюдён"."""
    return Verdict(
        indicator=indicator_number,
        name=name,
        verdict="ошибка",
        rationale=error_text[:_ERROR_RATIONALE_LIMIT],
        attempts=attempts,
    )


def _run_one_indicator(
    n: int,
    document_md: str,
    passport_block_text: str,
    run_id: str,
    timeout_s: int,
    max_budget_usd: float | None,
    extra_instruction: str | None = None,
    trace_prefix: str = "",
    regions: list[str] | None = None,
    search_limits: dict[int, int] | None = None,
) -> tuple[int, Verdict, float]:
    """
    Прогнать одного агента-индикатора. Никогда не бросает исключение наружу — любой
    сбой (провал runner.call, ошибка чтения промпта, необработанное исключение)
    превращается в вердикт "ошибка", чтобы не уронить весь параллельный прогон.

    Возвращает (номер индикатора, вердикт, стоимость вызова в USD).
    """
    logger = get_run_logger(run_id).getChild("03_verdicts")
    # Запасное имя на случай, если исключение случится ещё до чтения конфига индикатора
    name_fallback = f"индикатор {n}"

    try:
        indicator = get_indicator(n)
        name_fallback = indicator.name

        limit = (search_limits or SEARCH_LIMITS).get(n, SEARCH_LIMITS.get(n))
        prompt_text = fill_prompt_labels(prompt_path(n).read_text(encoding="utf-8"), regions, limit)
        instruction = append_task_clarification(prompt_text, extra_instruction)
        # 2.6: акт и паспорт — общий для всех 12 агентов текст — уходят в системный промпт,
        # чтобы агенты читали его из кэша друг у друга; в stdin ничего не идёт
        shared = _build_document(document_md, passport_block_text)
        allowed_tools, garant_tools = indicator_tools(n)

        result = call(
            instruction,
            None,
            INDICATOR_OUTPUT.get(n, VerdictOutput),   # 2.5: выжимка обязательна; 13.10: свой контракт индикатора
            run_id=run_id,
            trace_name=f"{trace_prefix}03_verdicts/{n:02d}.raw.json",
            allowed_tools=allowed_tools,
            timeout_s=max(timeout_s, INDICATOR_TIMEOUT_S.get(n, 0)),   # 2.7: индикатору 3 — больше
            max_budget_usd=max_budget_usd,
            shared_context=shared,
            mcp_tools=garant_tools or None,
        )

        if result.permission_denials:
            logger.warning(
                "indicator=%d permission_denials=%s", n, result.permission_denials,
            )

        if not result.ok or result.value is None:
            verdict = _error_verdict(
                n, indicator.name, result.error or "неизвестная ошибка", result.attempts,
            )
        else:
            # attempts всегда берём из CallResult, а не из ответа модели —
            # модель может подставить своё число, перезаписываем настоящим
            value, artifacts = _split_artifacts(result.value)
            if artifacts:
                save_json(run_id, f"{trace_prefix}03_verdicts/{n:02d}.checks.json", artifacts)
            verdict = value.model_copy(update={"attempts": result.attempts})

        return n, verdict, result.cost_usd

    except Exception as exc:  # исключение внутри потока не должно ронять весь прогон
        verdict = _error_verdict(n, name_fallback, f"необработанное исключение: {exc}", 1)
        return n, verdict, 0.0


def _start_delays(nums: list[int]) -> dict[int, float]:
    """
    Сдвиг старта агентов по группам кэша (2.6): первый индикатор группы — сразу,
    остальные — через CACHE_WARMUP_S. Инструменты стоят в начале кэша, поэтому кэш
    общий только у агентов с одинаковыми встроенными инструментами и (2.7) с Гарантом
    или без: описания инструментов Гаранта одни и те же при любом списке разрешённых.
    13.10: и с одинаковой схемой ответа — она тоже в начале запроса (инструмент StructuredOutput),
    поэтому индикатор со своим контрактом — своя группа.
    """
    delays: dict[int, float] = {}
    seen: set[object] = set()
    for n in nums:
        try:
            builtin, garant = indicator_tools(n)
            group: object = (tuple(builtin), bool(garant), INDICATOR_OUTPUT.get(n, VerdictOutput).__name__)
        except Exception:   # неизвестный номер — без сдвига; ошибку покажет сам агент
            group = f"?{n}"
        delays[n] = CACHE_WARMUP_S if group in seen else 0.0
        seen.add(group)
    return delays


def run_verdicts(
    document_md: str,
    run_id: str,
    *,
    passport: ActPassport | None = None,
    indicators: list[int] | None = None,
    max_workers: int = 12,
    timeout_s: int = 900,
    max_budget_usd: float | None = None,
    on_verdict: Callable[[Verdict], None] | None = None,
    extra_instruction: str | None = None,
    trace_prefix: str = "",
    save: bool = True,
    regions: list[str] | None = None,
    search_limits: dict[int, int] | None = None,
) -> list[Verdict]:
    """
    Параллельно прогнать агентов-индикаторов и собрать их вердикты (узел [2]).

    Барьер: управление возвращается только после того, как отработали ВСЕ
    запрошенные индикаторы — выход из `with ThreadPoolExecutor` дожидается
    завершения всех задач. Порядок результата — по номеру индикатора, а не по
    времени готовности (иначе трейсы будут прыгать между прогонами).

    indicators=None — прогнать все 12; список номеров — только перечисленные
    (для отладки).

    on_verdict — необязательный колбэк, вызываемый по мере готовности КАЖДОГО
    индикатора, до барьера (см. цикл ниже). Нужен оркестратору, чтобы страница
    прогона показывала "7 из 12 готово", а не ждала все двенадцать пять минут.
    По умолчанию None — без колбэка поведение узла не меняется ни в чём.

    extra_instruction, trace_prefix, save — для перепроверки из диалогового режима
    (план итерации 3, шаг 1.2): уточнение задачи к промпту, отдельная папка сырых
    ответов, возврат кандидата без записи 03_verdicts/NN.json (запись после
    «Принять»). Стоимость учитывается в meta всегда.

    regions — регионы сравнения прогона для промптов 7, 8, 11 (None — COMPARISON_REGIONS);
    search_limits — потолки поиска прогона по индикаторам (None — SEARCH_LIMITS).

    Требует, чтобы meta.json прогона уже существовал (создаётся trace.new_run()
    выше по пайплайну) — суммарная стоимость ПРИБАВЛЯЕТСЯ к записанной (_add_cost_to_meta).
    """
    nums = indicators if indicators is not None else [ind.number for ind in all_indicators()]
    logger = get_run_logger(run_id).getChild("03_verdicts")

    started = time.monotonic()
    verdicts_by_n: dict[int, Verdict] = {}
    cost_total = 0.0

    # Блок паспорта собирается один раз на все 12 агентов. Импорт внутри функции:
    # passport.py импортирует _add_cost_to_meta из hypotheses — кольцо на уровне модулей
    from app.nodes.passport import passport_block

    block = passport_block(passport)
    delays = _start_delays(nums)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _after, delays[n], _run_one_indicator, n, document_md, block, run_id, timeout_s,
                max_budget_usd, extra_instruction, trace_prefix, regions, search_limits,
            )
            for n in nums
        ]
        for future in as_completed(futures):
            n, verdict, cost = future.result()
            verdicts_by_n[n] = verdict
            cost_total += cost
            if save:
                save_json(run_id, f"03_verdicts/{n:02d}.json", verdict)
            # Событие готовности индикатора — по ходу, ДО барьера. Сбой колбэка
            # (например, ошибка записи файла прогресса на стороне оркестратора)
            # не должен ронять пятиминутный узел: ловим и идём дальше.
            if on_verdict is not None:
                try:
                    on_verdict(verdict)
                except Exception as exc:
                    logger.warning("on_verdict упал на индикаторе %d: %s", n, exc)

    ordered = [verdicts_by_n[n] for n in sorted(verdicts_by_n)]
    duration_total = time.monotonic() - started

    counts = Counter(v.verdict for v in ordered)
    logger.info(
        "verdicts done: violated=%d checked=%d not_applicable=%d no_data=%d errors=%d "
        "cost=$%.4f duration=%.1fs",
        counts.get("нарушен", 0), counts.get("соблюдён", 0), counts.get("не применимо", 0),
        counts.get("нет данных", 0), counts.get("ошибка", 0), cost_total, duration_total,
    )
    # Прибавляем, а не перезаписываем: в meta уже лежит стоимость паспорта, а в диалоге —
    # всего прогона (раньше здесь было update_meta(cost_usd=...), паспорт терялся).
    _add_cost_to_meta(run_id, cost_total)

    return ordered
