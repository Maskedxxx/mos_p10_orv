"""
Узлы [4а] и [4б] пайплайна П-10 — гипотезы из нарушений и гипотезы из пробелов в данных.

Механика обеих веток идентична и списана с узла [2] (app/nodes/verdicts.py):
ThreadPoolExecutor с барьером, один вызов модели на элемент кучи, провал одного
вызова не роняет остальные, порядок результата — по номеру индикатора. Различаются
только источник кучи (buckets.violated / buckets.no_data), файл промпта (04a/04b)
и подпись в логе. Поэтому публичных функций две (run_hypotheses_violated,
run_hypotheses_no_data) — по сигнатуре из ТЗ, — а общая механика вынесена в один
внутренний _run_hypotheses(), чтобы не дублировать барьер/сбор ошибок/трейсы дважды.
Флаг вместо двух функций был бы менее читаем на месте вызова в оркестраторе.

В модель уходит полный текст акта плюс карточка вердикта отдельным помеченным
JSON-блоком (см. _build_document) — вердикт не подмешивается в текст акта, модель
не должна его с ним перепутать. Инструменты не нужны ни одной из веток: гипотезы
пишутся по уже собранному вердикту и тексту акта, в интернет ходить незачем
(config.ALLOWED_TOOLS_TEXT — пустой список).

Провал вызова (CallResult.ok=False) или необработанное исключение внутри потока
НИКОГДА не превращается в выдуманную гипотезу — только в HypothesisSet с пустым
списком hypotheses и текстом ошибки в why_this_many. Это то же требование
владельца, что и в узле [2]: молчаливо проглоченный сбой был бы нечестным
результатом.

Стоимость вызовов узла суммируется и ПРИБАВЛЯЕТСЯ к уже записанной в meta.json
(узел [2] уже записал туда свою сумму — её нельзя затирать). Узлы [4а] и [4б]
могут быть запущены оркестратором одновременно (в разных потоках ОДНОГО процесса
— так же, как остальной пайплайн распараллеливается через ThreadPoolExecutor,
см. verdicts.py). Наивное чтение-изменение-запись meta.json из двух потоков —
классическая гонка: оба читают одно и то же старое значение cost_usd, поздняя
запись затирает более раннюю прибавку. Решение — блокировка (threading.Lock,
по образцу app/logger.py), а не "узлы возвращают стоимость, оркестратор пишет
meta" — второе потребовало бы менять сигнатуру функций (сшитую с ТЗ) на
кортеж (list[HypothesisSet], float) и знания об этом у оркестратора, а лока
внутри узла достаточно, пока оба узла живут в одном процессе (см. docstring
_add_cost_to_meta про границы применимости).
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import (
    ALLOWED_TOOLS_TEXT,
    NO_DATA_TEXT_FAMILY,
    PROMPT_HYPOTHESES_NO_DATA,
    PROMPT_HYPOTHESES_VIOLATED,
)
from app.contracts import ActPassport, Buckets, HypothesisSet, HypothesisSetOutput, RunMeta, Verdict
from app.logger import get_run_logger
from app.runner import call
from app.trace import load_json, save_json, update_meta

# Длина, до которой обрезается текст ошибки в why_this_many при провале вызова
_ERROR_TEXT_LIMIT = 2000

# Лок для потокобезопасного прибавления стоимости к meta.json — см. docstring
# модуля и _add_cost_to_meta про причину и границы применимости.
_meta_lock = threading.Lock()


def append_task_clarification(instruction: str, extra_instruction: str | None) -> str:
    """
    Дописать к промпту узла уточнение задачи от пользователя (диалоговый режим,
    план итерации 3, шаг 1.2). Без уточнения промпт возвращается побайтно прежним.

    Уточнение говорит, ЧТО ещё проверить и ГДЕ искать, но не какой вывод нужен:
    это правило промпта оркестратора, а текст уточнения пользователь видит
    в плане до подтверждения.
    """
    if not extra_instruction:
        return instruction
    return (
        f"{instruction}\n\n<уточнение_задачи>\n"
        "Пользователь уточнил объём проверки. Выполни инструкцию выше полностью "
        "и дополнительно учти уточнение. Оно говорит, что проверить и где искать, "
        "а не к какому выводу прийти.\n\n"
        f"{extra_instruction.strip()}\n</уточнение_задачи>\n"
    )


def _add_cost_to_meta(run_id: str, cost_delta: float) -> None:
    """
    Прибавить стоимость вызовов узла к уже записанной в meta.json, не затирая её.

    Лок сериализует чтение+запись между узлами [4а] и [4б], если оба вызваны
    оркестратором параллельно в потоках одного процесса. Если оркестратор
    когда-нибудь станет запускать узлы отдельными процессами — thread-lock не
    защитит, потребуется файловая блокировка (например fcntl.flock).
    """
    with _meta_lock:
        current = load_json(run_id, "meta.json", RunMeta)
        update_meta(run_id, cost_usd=current.cost_usd + cost_delta)


def _build_document(document_md: str, passport_block_text: str, verdict: Verdict) -> str:
    """
    Собрать документ для stdin агента: полный текст акта, блок паспорта (если
    есть), карточка вердикта — три помеченных блока в порядке, описанном
    во входных данных промптов 04a/04b v2. Карточка сериализуется читаемо
    (indent=2, кириллица без escape).
    """
    verdict_json = json.dumps(verdict.model_dump(mode="json"), ensure_ascii=False, indent=2)
    parts = ["## ПОЛНЫЙ ТЕКСТ АКТА\n\n" f"{document_md}\n"]
    if passport_block_text:
        parts.append(passport_block_text)
    parts.append("## КАРТОЧКА ВЕРДИКТА (JSON)\n\n" f"```json\n{verdict_json}\n```\n")
    return "\n".join(parts)


def _empty_set(verdict: Verdict, error_text: str) -> HypothesisSet:
    """Пустой набор гипотез при провале вызова — провал не выдумывает гипотезу."""
    return HypothesisSet(
        indicator=verdict.indicator,
        name=verdict.name,
        why_this_many=error_text[:_ERROR_TEXT_LIMIT],
        hypotheses=[],
    )


def _run_one_hypothesis(
    verdict: Verdict,
    document_md: str,
    passport_block_text: str,
    run_id: str,
    instruction: str,
    timeout_s: int,
    max_budget_usd: float | None,
    trace_prefix: str = "",
) -> tuple[int, HypothesisSet, float, bool]:
    """
    Прогнать одного агента-гипотезу по одному вердикту. Никогда не бросает
    исключение наружу — любой сбой (провал call(), необработанное исключение)
    превращается в пустой HypothesisSet с текстом ошибки в why_this_many, чтобы
    не уронить весь параллельный прогон.

    Возвращает (номер индикатора, набор гипотез, стоимость вызова, признак успеха).
    Признак успеха нужен отдельно от "hypotheses пуст" — пустой список гипотез
    может быть и честным результатом успешного вызова (модель не нашла, что
    добавить), а не только следствием провала.
    """
    n = verdict.indicator
    try:
        document = _build_document(document_md, passport_block_text, verdict)

        result = call(
            instruction,
            document,
            HypothesisSetOutput,   # 2.5: выжимки для эксперта в ответе обязательны
            run_id=run_id,
            trace_name=f"{trace_prefix}05_hypotheses/{n:02d}.raw.json",
            allowed_tools=ALLOWED_TOOLS_TEXT,
            timeout_s=timeout_s,
            max_budget_usd=max_budget_usd,
        )

        if not result.ok or result.value is None:
            return n, _empty_set(verdict, result.error or "неизвестная ошибка"), result.cost_usd, False

        # indicator/name перезаписываем из вердикта (источник истины), чтобы модель
        # не могла разъехаться с номером/именем индикатора, под которым сохраняется
        # артефакт NN.json — по аналогии с перезаписью attempts в узле [2]
        hset = result.value.model_copy(update={"indicator": n, "name": verdict.name})
        return n, hset, result.cost_usd, True

    except Exception as exc:  # исключение внутри потока не должно ронять весь прогон
        return n, _empty_set(verdict, f"необработанное исключение: {exc}"), 0.0, False


def _run_hypotheses(
    verdicts: list[Verdict],
    document_md: str,
    passport_block_text: str,
    run_id: str,
    prompt_file: Path,
    branch_label: str,
    max_workers: int,
    timeout_s: int,
    max_budget_usd: float | None,
    extra_instruction: str | None = None,
    trace_prefix: str = "",
    save: bool = True,
) -> list[HypothesisSet]:
    """
    Общая механика узлов [4а]/[4б]: параллельный прогон по своей куче вердиктов.

    extra_instruction, trace_prefix, save — для перезапуска из диалогового режима:
    уточнение задачи к промпту, отдельная папка сырых ответов, возврат кандидата
    без записи 05_hypotheses/NN.json (запись после «Принять»). Стоимость
    учитывается в meta всегда.
    """
    logger = get_run_logger(run_id).getChild(f"05_hypotheses_{branch_label}")
    instruction = append_task_clarification(prompt_file.read_text(encoding="utf-8"), extra_instruction)

    started = time.monotonic()
    sets_by_n: dict[int, HypothesisSet] = {}
    cost_total = 0.0
    failed_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _run_one_hypothesis, verdict, document_md, passport_block_text, run_id, instruction,
                timeout_s, max_budget_usd, trace_prefix,
            )
            for verdict in verdicts
        ]
        for future in as_completed(futures):
            n, hset, cost, ok = future.result()
            sets_by_n[n] = hset
            cost_total += cost
            if not ok:
                failed_count += 1
            if save:
                save_json(run_id, f"05_hypotheses/{n:02d}.json", hset)

    ordered = [sets_by_n[n] for n in sorted(sets_by_n)]
    duration_total = time.monotonic() - started
    total_hypotheses = sum(len(s.hypotheses) for s in ordered)

    logger.info(
        "hypotheses done (%s): sets=%d hypotheses=%d failed=%d cost=$%.4f duration=%.1fs",
        branch_label, len(ordered), total_hypotheses, failed_count, cost_total, duration_total,
    )

    _add_cost_to_meta(run_id, cost_total)

    return ordered


def run_hypotheses_violated(
    buckets: Buckets,
    document_md: str,
    run_id: str,
    *,
    passport: ActPassport | None = None,
    max_workers: int = 12,
    timeout_s: int = 900,
    max_budget_usd: float | None = None,
    extra_instruction: str | None = None,
    trace_prefix: str = "",
    save: bool = True,
) -> list[HypothesisSet]:
    """
    Узел [4а] — гипотезы из нарушений: один вызов модели на каждый вердикт
    "нарушен" из buckets.violated, параллельно (ThreadPoolExecutor, барьер —
    возврат только после завершения всех задач).

    Возвращает список HypothesisSet, по одному на вердикт кучи violated,
    в порядке номера индикатора. Провал вызова НЕ роняет прогон и НЕ выдумывает
    гипотезу (см. docstring модуля и _run_one_hypothesis).

    Стоимость вызовов складывается и ПРИБАВЛЯЕТСЯ к уже записанной в meta.json.
    Может быть вызвана оркестратором одновременно с run_hypotheses_no_data —
    гонка при записи в meta.json устранена локом, см. docstring модуля и
    _add_cost_to_meta.
    """
    from app.nodes.passport import passport_block  # кольцевой импорт: passport.py берёт отсюда _add_cost_to_meta

    return _run_hypotheses(
        buckets.violated, document_md, passport_block(passport), run_id, PROMPT_HYPOTHESES_VIOLATED, "violated",
        max_workers, timeout_s, max_budget_usd, extra_instruction, trace_prefix, save,
    )


def run_hypotheses_no_data(
    buckets: Buckets,
    document_md: str,
    run_id: str,
    *,
    passport: ActPassport | None = None,
    max_workers: int = 12,
    timeout_s: int = 900,
    max_budget_usd: float | None = None,
    extra_instruction: str | None = None,
    trace_prefix: str = "",
    save: bool = True,
) -> list[HypothesisSet]:
    """
    Узел [4б] — гипотезы из пробелов в данных: один вызов модели на каждый
    вердикт "нет данных" из buckets.no_data, параллельно (ThreadPoolExecutor).

    Механика идентична run_hypotheses_violated (см. её docstring и docstring
    модуля) — отличаются только куча вердиктов и промпт (04b вместо 04a).

    Индикаторы из NO_DATA_TEXT_FAMILY (1-6) пропускаются: их «нет данных» —
    незавершённая проверка по тексту или правовой базе, а не пробел в практике;
    гипотезы по ним не строятся, в Excel такой вердикт идёт строкой «проверка
    не завершена» (ОС экспертов, Б1: МАДИ инд. 3 и «зелёные» инд. 4 дали ложные
    гипотезы с оценками 1).
    """
    from app.nodes.passport import passport_block  # кольцевой импорт, см. run_hypotheses_violated

    practice = [v for v in buckets.no_data if v.indicator not in NO_DATA_TEXT_FAMILY]
    skipped = [v.indicator for v in buckets.no_data if v.indicator in NO_DATA_TEXT_FAMILY]
    if skipped:
        get_run_logger(run_id).getChild("05_hypotheses_no_data").info(
            "no_data: индикаторы %s пропущены — нехватка текста/правовой базы, гипотезы не строятся",
            skipped,
        )
    return _run_hypotheses(
        practice, document_md, passport_block(passport), run_id, PROMPT_HYPOTHESES_NO_DATA, "no_data",
        max_workers, timeout_s, max_budget_usd, extra_instruction, trace_prefix, save,
    )
