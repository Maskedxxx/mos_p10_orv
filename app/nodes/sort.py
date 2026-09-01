"""
Узел [3] пайплайна П-10 — разбор корзины вердиктов на четыре кучи.

Чистый код, без обращения к модели. Раскладывает вердикты (по числу индикаторов,
обычно двенадцать) по кучам в зависимости от значения поля verdict:
"нарушен" -> кандидаты на гипотезы о нарушении (дальше — узел [4а]), "нет данных" ->
кандидаты на гипотезы о пробеле в данных (узел [4б]), "соблюдён" и "не применимо" ->
одна общая куча "проверено" (в карту гипотез не идут, попадают в чек-лист),
"ошибка" -> провал агента-индикатора на узле [2] (тоже без дальнейшей обработки
здесь — это сигнал, что индикатор не удалось оценить, а не то, что он нарушен
или соблюдён).

Узел ничего не дописывает к вердиктам и ничего не переоценивает — только
раскладывает по кучам то, что уже посчитано узлом [2].
"""

from __future__ import annotations

from app.contracts import Buckets, Verdict
from app.logger import get_run_logger
from app.trace import save_json


def sort_verdicts(verdicts: list[Verdict], run_id: str) -> Buckets:
    """
    Разложить вердикты по четырём кучам (узел [3]).

    Порядок внутри каждой кучи — по номеру индикатора (не по порядку входного
    списка). Ничего не теряется: VerdictValue — исчерпывающий Literal из пяти
    значений ("нарушен"/"соблюдён"/"не применимо"/"нет данных"/"ошибка"), каждое
    из них попадает ровно в одну кучу, поэтому сумма длин четырёх куч по
    построению равна числу вердиктов на входе. assert ниже — защита на случай,
    если Literal когда-нибудь расширят новым значением, а разбор здесь забудут
    обновить: тогда вердикт молча потерялся бы, а не свалился бы явной ошибкой.

    Сохраняет результат в трейс (04_buckets.json) и пишет в лог сводку по кучам.
    """
    violated = sorted((v for v in verdicts if v.verdict == "нарушен"), key=lambda v: v.indicator)
    no_data = sorted((v for v in verdicts if v.verdict == "нет данных"), key=lambda v: v.indicator)
    checked = sorted(
        (v for v in verdicts if v.verdict in ("соблюдён", "не применимо")),
        key=lambda v: v.indicator,
    )
    failed = sorted((v for v in verdicts if v.verdict == "ошибка"), key=lambda v: v.indicator)

    buckets = Buckets(violated=violated, no_data=no_data, checked=checked, failed=failed)

    total_out = len(violated) + len(no_data) + len(checked) + len(failed)
    assert total_out == len(verdicts), (
        f"разбор корзины потерял вердикты: вход={len(verdicts)}, распределено={total_out}"
    )

    save_json(run_id, "04_buckets.json", buckets)

    logger = get_run_logger(run_id).getChild("04_buckets")
    logger.info(
        "sort done: violated=%d no_data=%d checked=%d failed=%d",
        len(violated), len(no_data), len(checked), len(failed),
    )

    return buckets
