"""
Операции диалогового режима (план итерации 3, шаг 3).

Модель ничего не пишет сама: оркестратор предлагает план из закрытого списка
операций (app/contracts.py: EditText … AppendRequest), пользователь подтверждает,
код выполняет по одной операции. Реестры ниже — «вид операции → функция»; новая
операция — одна функция и одна строка в реестре, узлы пайплайна не меняются.

Операция без модели сразу даёт новую версию: правка артефактов → пересборка
Excel (код, секунды) → снимок версии, строка журнала, пометки (app/versions.py).
Операция с моделью (перепроверка, досчёт гипотез, дополнение запроса) сначала
возвращает кандидата «было — стало»; записывается он только после «Принять»
(accept), «Отклонить» не пишет ничего. Сбой посреди фиксации возвращает корень
прогона к текущей версии.

Каскад — простейший вариант, решение владельца 2026-09-22: считается только то,
о чём попросили; затронутое помечается «устарело» (Marks), досчёт предлагает
оркестратор следующим шагом.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import ValidationError

from app import versions, config
from app.config import NO_DATA_TEXT_FAMILY
from app.contracts import (
    ActPassport, AddMapRow, AppendRequest, BuildHypotheses, Buckets, DataRequest, DeleteMapRow, DeleteRequestItem,
    MarkNotTracked,
    EditText, HypothesisMap, HypothesisSet, JournalEntry, MapRow, MergeMapRows, Operation, RecheckIndicator,
    RequestItem, RunMeta, RunProgress, SetVerdict, SplitRequestItem, Verdict, VersionState,
)
from app.nodes.excel import points_text, unverified_verdicts, build_excel, violated_without_rows
from app.nodes.hypotheses import run_hypotheses_no_data, run_hypotheses_violated
from app.nodes.merge import clause_tokens, fallback_map, flatten_hypotheses
from app.nodes.request import build_request
from app.nodes.sort import sort_verdicts
from app.nodes.verdicts import run_verdicts
from app.trace import (
    BUCKETS_FILE, DOCUMENT_FILE, HYPOTHESES_DIR, MAP_FILE, META_FILE, PASSPORT_FILE, PROGRESS_FILE, REQUEST_FILE,
    VERDICTS_DIR, VERSIONS_DIR, load_json, run_dir, save_bytes, save_json,
)


class OperationError(Exception):
    """Операцию выполнить нельзя — текст объясняет пользователю почему; ничего не записано."""


@dataclass
class Change:
    """Что сделала операция — для журнала и для чата."""

    target: str
    action: str
    before: str | None = None
    after: str | None = None


@dataclass
class Candidate:
    """Результат модели, ждущий решения пользователя: «Принять» или «Отклонить»."""

    op: Operation
    payload: object
    before: str
    after: str
    cost_usd: float
    checks: bytes | None = None  # таблица перепроверки — в корень при «Принять»
    cost_known: bool = True


@dataclass
class StepResult:
    """Итог шага плана: новая версия (version) либо кандидат на решение (candidate)."""

    version: int | None
    change: Change | None = None
    candidate: Candidate | None = None
    cost_usd: float = 0.0
    cost_known: bool = True


@dataclass
class _Ctx:
    """Артефакты текущей версии прогона; операции меняют их в памяти, фиксирует _commit."""

    run_id: str
    state: VersionState
    verdicts: dict[int, Verdict]
    hyp_map: HypothesisMap
    request: DataRequest
    passport: ActPassport | None
    document_md: str
    new_sets: dict[int, HypothesisSet] = field(default_factory=dict)


def _load(run_id: str, state: VersionState) -> _Ctx:
    root = run_dir(run_id)
    verdicts = {int(p.stem): load_json(run_id, f"{VERDICTS_DIR}/{p.name}", Verdict)
                for p in sorted((root / VERDICTS_DIR).glob("[0-9][0-9].json"))}
    passport = load_json(run_id, PASSPORT_FILE, ActPassport) if (root / PASSPORT_FILE).is_file() else None
    document = (root / DOCUMENT_FILE).read_text(encoding="utf-8") if (root / DOCUMENT_FILE).is_file() else ""
    return _Ctx(run_id, state.model_copy(deep=True), verdicts, load_json(run_id, MAP_FILE, HypothesisMap),
                load_json(run_id, REQUEST_FILE, DataRequest), passport, document)


def _valid(model_cls, obj, **update):
    """Пересобрать объект с правкой через валидацию контракта — правка не обходит его правил."""
    try:
        return model_cls.model_validate({**obj.model_dump(), **update})
    except ValidationError as exc:
        raise OperationError(f"правка нарушает правила: {exc.errors()[0]["msg"]}") from exc


def _row(ctx: _Ctx, row_id: int) -> MapRow:
    row = next((r for r in ctx.hyp_map.rows if r.row_id == row_id), None)
    if row is None:
        raise OperationError(f"строки карты {row_id} нет")
    return row


def _item(ctx: _Ctx, num: str) -> RequestItem:
    item = next((it for it in ctx.request.items if it.num == num), None)
    if item is None:
        raise OperationError(f"позиции запроса № {num} нет")
    return item


def _set_rows(ctx: _Ctx, rows: list[MapRow]) -> None:
    ctx.hyp_map = ctx.hyp_map.model_copy(update={"rows": sorted(rows, key=lambda r: r.row_id)})


def _set_items(ctx: _Ctx, items: list[RequestItem]) -> None:
    ctx.request = ctx.request.model_copy(update={"items": items})


def _in_work(indicator: int, verdict: str) -> bool:
    """По вердикту строятся гипотезы: «нарушен» и «нет данных» практических индикаторов."""
    return verdict == "нарушен" or (verdict == "нет данных" and indicator not in NO_DATA_TEXT_FAMILY)


def _add_once(values: list, value) -> None:
    if value not in values:
        values.append(value)


def _needs_request(row: MapRow) -> bool:
    # 2.7 (Ж2): вопрос органу словами — тоже позиция бланка (лист «Вопросы органу»)
    return any(dp.source in ("запрос органу", "вопрос органу") for dp in row.data)


# ---- операции без модели -------------------------------------------------

_MAP_FIELDS = {"гипотеза": "hypothesis", "основание": "grounds"}
_EFFECT_FIELDS = {"показатель эффекта": "metric", "как считать": "how_to_calculate", "что подтвердит": "expected_trend"}
_ITEM_FIELDS = {"показатель": "metric", "пояснение": "note", "пункт": "clause"}
# 2.5: эксперт видит в Excel выжимку — правка текста правит её, полный текст агента остаётся для анализа
_BRIEF_OF = {"hypothesis": "hypothesis_brief", "grounds": "grounds_brief",
             "how_to_calculate": "how_to_calculate_brief", "expected_trend": "expected_trend_brief"}
_BULLET = "•-—*· \t"


def _split_points(text: str) -> list[str]:
    """Текст пользователя — пункты выжимки: по строке на пункт, без маркеров и пустых строк."""
    points = [line.strip().lstrip(_BULLET).strip() for line in text.splitlines()]
    return [p for p in points if p] or [text.strip()]


def _edit_text(ctx: _Ctx, op: EditText) -> Change:
    marks = ctx.state.marks
    if op.sheet == "вердикт":
        n = int(op.key)
        if n not in ctx.verdicts:
            raise OperationError(f"индикатора {n} нет")
        old = ctx.verdicts[n]
        points = _split_points(op.new_text)
        ctx.verdicts[n] = _valid(Verdict, old, rationale_brief=points)
        _add_once(marks.edited_verdicts, n)
        return Change(f"Вердикты, инд. {n}", "обоснование переформулировано",
                      points_text(old.rationale_brief, old.rationale), points_text(points, ""))
    if op.sheet == "строка карты":
        row = _row(ctx, int(op.key))
        if op.field in _MAP_FIELDS:
            full = _MAP_FIELDS[op.field]
            before = points_text(getattr(row, _BRIEF_OF[full]), getattr(row, full))
            after = _split_points(op.new_text)
            new_row = _valid(MapRow, row, **{_BRIEF_OF[full]: after})
            after = points_text(after, "")
        else:
            if op.effect_index > len(row.effects):
                raise OperationError(f"у строки {row.row_id} нет эффекта № {op.effect_index}")
            effects = [e.model_dump() for e in row.effects]
            effect, full = effects[op.effect_index - 1], _EFFECT_FIELDS[op.field]
            if full in _BRIEF_OF:   # «как считать», «что подтвердит» — выжимка
                before = points_text(effect[_BRIEF_OF[full]], effect[full])
                effect[_BRIEF_OF[full]] = _split_points(op.new_text)
                after = points_text(effect[_BRIEF_OF[full]], "")
            else:                   # название показателя — одно, выжимки у него нет
                before, after = effect[full], op.new_text
                effect[full] = op.new_text
            new_row = _valid(MapRow, row, effects=effects)
        _set_rows(ctx, [new_row if r.row_id == row.row_id else r for r in ctx.hyp_map.rows])
        _add_once(marks.edited_rows, row.row_id)
        return Change(f"Карта гипотез, стр. {row.row_id}", f"переформулировано поле «{op.field}»", before, after)
    item = _item(ctx, op.key)
    before = getattr(item, _ITEM_FIELDS[op.field])
    new_item = _valid(RequestItem, item, **{_ITEM_FIELDS[op.field]: op.new_text})
    _set_items(ctx, [new_item if it.num == item.num else it for it in ctx.request.items])
    _add_once(marks.edited_items, item.num)
    return Change(f"Запрос показателей, № {item.num}", f"переформулировано поле «{op.field}»", before, op.new_text)


def _apply_verdict(ctx: _Ctx, new: Verdict) -> str:
    """Поставить вердикт и отметить каскад; вернуть прежнее значение."""
    n, old = new.indicator, ctx.verdicts[new.indicator]
    ctx.verdicts[n] = new
    marks = ctx.state.marks
    rows = [r.row_id for r in ctx.hyp_map.rows if n in r.indicators]
    if old.verdict != new.verdict:
        for r in rows:
            marks.stale_rows[r] = f"вердикт инд. {n} изменён: «{old.verdict}» → «{new.verdict}»"
    if _in_work(n, new.verdict) and not rows:
        marks.stale_verdicts[n] = "гипотез по индикатору нет — можно построить"
    else:
        marks.stale_verdicts.pop(n, None)
    return old.verdict


def _set_verdict(ctx: _Ctx, op: SetVerdict) -> Change:
    old = ctx.verdicts[op.indicator]
    new = Verdict(indicator=op.indicator, name=old.name, verdict=op.verdict, rationale=op.rationale,
                  rationale_brief=_split_points(op.rationale),   # выжимка — слова эксперта
                  missing_data=op.missing_data, proof=old.proof, clause=old.clause, sources=old.sources)
    before = _apply_verdict(ctx, new)
    _add_once(ctx.state.marks.edited_verdicts, op.indicator)
    return Change(f"Вердикты, инд. {op.indicator}", "вердикт изменён по решению эксперта", before, op.verdict)


def _drop_row_from_request(ctx: _Ctx, row_ids: set[int], keep: int | None, why: str) -> None:
    """Убрать строки карты из ссылок запроса; позиция без ссылок — «устарела». keep — куда перевести ссылки."""
    items = []
    for it in ctx.request.items:
        if not set(it.for_rows) & row_ids:
            items.append(it)
            continue
        sources = [s.model_copy(update={"row_id": keep}) if s.row_id in row_ids and keep else s
                   for s in it.sources if s.row_id not in row_ids or keep]
        if not sources:
            ctx.state.marks.stale_items[it.num] = why
            items.append(it)
            continue
        for_rows = sorted({s.row_id for s in sources})
        items.append(_valid(RequestItem, it, for_rows=for_rows, sources=[s.model_dump() for s in sources]))
    _set_items(ctx, items)


def _forget_row_marks(ctx: _Ctx, row_ids: set[int]) -> None:
    marks = ctx.state.marks
    marks.edited_rows = [r for r in marks.edited_rows if r not in row_ids]
    for r in row_ids:
        marks.stale_rows.pop(r, None)


def _delete_map_row(ctx: _Ctx, op: DeleteMapRow) -> Change:
    row = _row(ctx, op.row_id)
    _set_rows(ctx, [r for r in ctx.hyp_map.rows if r.row_id != op.row_id])
    _drop_row_from_request(ctx, {op.row_id}, None, f"строка карты {op.row_id} удалена — позиция, возможно, лишняя")
    _forget_row_marks(ctx, {op.row_id})
    return Change(f"Карта гипотез, стр. {op.row_id}", "строка удалена по решению эксперта", row.hypothesis, None)


def _new_row_id(ctx: _Ctx) -> int:
    row_id = ctx.state.next_row_id
    ctx.state.next_row_id += 1
    return row_id


def _add_map_row(ctx: _Ctx, op: AddMapRow) -> Change:
    try:
        row = MapRow(row_id=_new_row_id(ctx), indicators=op.indicators, hypothesis=op.hypothesis, grounds=op.grounds,
                     effects=op.effects, data=op.data, proof=op.proof, clause=op.clause)
    except ValidationError as exc:
        raise OperationError(f"строка не проходит правила карты: {exc.errors()[0]["msg"]}") from exc
    _set_rows(ctx, ctx.hyp_map.rows + [row])
    _add_once(ctx.state.marks.edited_rows, row.row_id)
    if _needs_request(row):
        ctx.state.marks.stale_rows[row.row_id] = "нет позиций запроса — можно дополнить запрос"
    return Change(f"Карта гипотез, стр. {row.row_id}", "строка добавлена по формулировке эксперта", None, row.hypothesis)


def _merge_map_rows(ctx: _Ctx, op: MergeMapRows) -> Change:
    ids = sorted(set(op.row_ids))
    rows = [_row(ctx, r) for r in ids]
    keep, gone = ids[0], set(ids[1:])
    effects, data = {}, {}
    for r in rows:
        for e in r.effects:
            effects.setdefault(e.affected, e)          # один эффект на группу — первый
        for dp in r.data:
            data.setdefault((dp.metric.strip().lower(), dp.source), dp)
    clauses = [r.clause for r in rows if r.clause]
    merged = MapRow(
        row_id=keep, indicators=sorted({n for r in rows for n in r.indicators}), hypothesis=op.hypothesis,
        grounds=" | ".join(r.grounds for r in rows), effects=list(effects.values()), data=list(data.values()),
        proof=next((r.proof for r in rows if r.proof), None), clause=", ".join(dict.fromkeys(clauses)) or None,
        hypothesis_brief=_split_points(op.hypothesis),
        grounds_brief=list(dict.fromkeys(p for r in rows for p in (r.grounds_brief or [r.grounds]))),
    )
    _set_rows(ctx, [merged] + [r for r in ctx.hyp_map.rows if r.row_id not in ids])
    _drop_row_from_request(ctx, gone, keep, "")
    _forget_row_marks(ctx, gone)
    _add_once(ctx.state.marks.edited_rows, keep)
    return Change(f"Карта гипотез, стр. {', '.join(map(str, ids))}", f"строки слиты в стр. {keep} по решению человека",
                  " | ".join(r.hypothesis for r in rows), op.hypothesis)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _mark_not_tracked(ctx: _Ctx, op: MarkNotTracked) -> Change:
    """
    Ж3 (2.7): эксперт сказал, что такую цифру не ведёт никто. Источник цифры — «нигде
    не ведётся»; цифра уходит из позиций бланка, позиция без единой цифры удаляется
    (у органа нечего спрашивать), кроме позиции с обязательным показателем — она остаётся
    без ссылки на цифру (15.10). «Как считать» переписывает следующая операция плана —
    «правка текста» (промпт диалога).
    """
    row = _row(ctx, op.row_id)
    key = _norm(op.metric)
    idx = next((i for i, dp in enumerate(row.data) if _norm(dp.metric) == key), None)
    if idx is None:
        raise OperationError(f"в строке {op.row_id} нет цифры «{op.metric}»")
    old = row.data[idx]
    data = [dp.model_dump() for dp in row.data]
    data[idx] = {**data[idx], "source": "нигде не ведётся", "where": None}
    _set_rows(ctx, [r for r in ctx.hyp_map.rows if r.row_id != op.row_id] + [_valid(MapRow, row, data=data)])
    items, dropped = [], []
    for it in ctx.request.items:
        sources = [s for s in it.sources if not (s.row_id == op.row_id and _norm(s.metric) == key)]
        if len(sources) == len(it.sources):
            items.append(it)
        elif not sources and it.mandatory and it.kind != "открытый вопрос":
            # обязательный показатель в бланке всегда (Р4): остаётся, только без ссылки на эту цифру (15.10);
            # открытый вопрос без строки карты невозможен — он уходит вместе с цифрой
            items.append(_valid(RequestItem, it, for_rows=[], sources=[]))
        elif not sources:
            dropped.append(it.num)
            ctx.state.marks.stale_items.pop(it.num, None)
        else:
            items.append(_valid(RequestItem, it, for_rows=sorted({s.row_id for s in sources}),
                                sources=[s.model_dump() for s in sources]))
    _set_items(ctx, items)
    ctx.state.marks.edited_items = [n for n in ctx.state.marks.edited_items if n not in dropped]
    _add_once(ctx.state.marks.edited_rows, op.row_id)
    gone = f"; из бланка убрана позиция № {', '.join(dropped)}" if dropped else ""
    return Change(f"Карта гипотез, стр. {op.row_id}", f"цифра помечена: нигде не ведётся{gone}",
                  f"{old.metric} — {old.source}", "нигде не ведётся")


def _delete_request_item(ctx: _Ctx, op: DeleteRequestItem) -> Change:
    item = _item(ctx, op.num)
    _set_items(ctx, [it for it in ctx.request.items if it.num != op.num])
    marks = ctx.state.marks
    marks.edited_items = [n for n in marks.edited_items if n != op.num]
    marks.stale_items.pop(op.num, None)
    return Change(f"Запрос показателей, № {op.num}", "позиция удалена по решению эксперта", item.metric, None)


def _split_request_item(ctx: _Ctx, op: SplitRequestItem) -> Change:
    item = _item(ctx, op.num)
    parts = []
    for part in op.parts:
        num = str(ctx.state.next_item_num)
        ctx.state.next_item_num += 1
        parts.append(_valid(RequestItem, item, num=num, metric=part.metric, unit=part.unit, note=None, breakdown=[]))
    i = ctx.request.items.index(item)
    _set_items(ctx, ctx.request.items[:i] + parts + ctx.request.items[i + 1:])
    marks = ctx.state.marks
    marks.edited_items = [n for n in marks.edited_items if n != op.num] + [p.num for p in parts]
    marks.stale_items.pop(op.num, None)
    return Change(f"Запрос показателей, № {op.num}", f"позиция разделена на № {', '.join(p.num for p in parts)}",
                  item.metric, " | ".join(p.metric for p in parts))


# ---- операции с моделью: кандидат → «Принять» ------------------------------

def _meta_cost(run_id: str) -> float:
    return load_json(run_id, META_FILE, RunMeta).cost_usd


def _prepare_recheck(ctx: _Ctx, op: RecheckIndicator, prefix: str) -> Candidate:
    start = _meta_cost(ctx.run_id)
    meta = load_json(ctx.run_id, META_FILE, RunMeta)   # настройки прогона — те же, что при разборе (2.7)
    new = run_verdicts(ctx.document_md, ctx.run_id, passport=ctx.passport, indicators=[op.indicator],
                       extra_instruction=op.clarification, trace_prefix=prefix, save=False,
                       regions=meta.comparison_regions, search_limits=meta.search_limits)[0]
    cost = _meta_cost(ctx.run_id) - start
    if new.verdict == "ошибка":
        raise OperationError(f"перепроверка не удалась: {new.rationale[:300]}")
    old = ctx.verdicts[op.indicator]
    checks_path = run_dir(ctx.run_id) / f"{prefix}{VERDICTS_DIR}/{op.indicator:02d}.checks.json"
    return Candidate(op, new, f"{old.verdict}\n{points_text(old.rationale_brief, old.rationale)}",
                     f"{new.verdict}\n{points_text(new.rationale_brief, new.rationale)}", cost,
                     checks=checks_path.read_bytes() if checks_path.is_file() else None, cost_known=config.LLM_BACKEND == "claude")


def _accept_recheck(ctx: _Ctx, cand: Candidate) -> Change:
    new: Verdict = cand.payload
    before = _apply_verdict(ctx, new)
    if cand.checks is not None:   # таблица проверки — та, на которой стоит новый вердикт
        save_bytes(ctx.run_id, f"{VERDICTS_DIR}/{new.indicator:02d}.checks.json", cand.checks)
    return Change(f"Вердикты, инд. {new.indicator}", "вердикт перепроверен агентом индикатора", before, new.verdict)


# Цель записи журнала «гипотезы построены» — её читает и _run_gaps
_BUILT_PREFIX = "Карта гипотез, инд. "
_BUILT_TARGET = re.compile(re.escape(_BUILT_PREFIX) + r"(\d+)")


def _prepare_hypotheses(ctx: _Ctx, op: BuildHypotheses, prefix: str) -> Candidate:
    v = ctx.verdicts[op.indicator]
    if not _in_work(op.indicator, v.verdict):
        raise OperationError(
            f"по индикатору {op.indicator} гипотезы не строятся: вердикт «{v.verdict}»"
            + (" — для индикаторов 1–6 это строка «нужна ручная проверка»" if v.verdict == "нет данных" else "")
        )
    node = run_hypotheses_violated if v.verdict == "нарушен" else run_hypotheses_no_data
    buckets = Buckets(violated=[v]) if v.verdict == "нарушен" else Buckets(no_data=[v])
    start = _meta_cost(ctx.run_id)
    hset = node(buckets, ctx.document_md, ctx.run_id, passport=ctx.passport,
                extra_instruction=op.clarification, trace_prefix=prefix, save=False)[0]
    cost = _meta_cost(ctx.run_id) - start
    if not hset.hypotheses:
        raise OperationError(f"агент гипотез не предложил ни одной: {hset.why_this_many[:400]}")
    have = [r.row_id for r in ctx.hyp_map.rows if op.indicator in r.indicators]
    after = "\n".join(f"— {h.hypothesis}" for h in hset.hypotheses)
    return Candidate(op, hset, f"строк карты по индикатору {op.indicator}: {len(have)}", after, cost, cost_known=config.LLM_BACKEND == "claude")


def _accept_hypotheses(ctx: _Ctx, cand: Candidate) -> Change:
    hset: HypothesisSet = cand.payload
    rows = [row.model_copy(update={"row_id": _new_row_id(ctx)}) for row in fallback_map(flatten_hypotheses([hset])).rows]
    near = []
    for row in rows:
        same = [r.row_id for r in ctx.hyp_map.rows if clause_tokens(r.clause) & clause_tokens(row.clause)]
        if same:
            near.append(f"стр. {row.row_id} — тот же пункт, что у стр. {', '.join(map(str, same))}: проверьте, не дубль ли")
        if _needs_request(row):
            ctx.state.marks.stale_rows[row.row_id] = "нет позиций запроса — можно дополнить запрос"
    _set_rows(ctx, ctx.hyp_map.rows + rows)
    ctx.new_sets[hset.indicator] = hset
    ctx.state.marks.stale_verdicts.pop(hset.indicator, None)
    after = "; ".join(f"стр. {r.row_id}" for r in rows) + ("\n" + "\n".join(near) if near else "")
    return Change(f"{_BUILT_PREFIX}{hset.indicator}", "гипотезы построены агентом гипотез", None, after)


def _prepare_request(ctx: _Ctx, op: AppendRequest, prefix: str) -> Candidate:
    rows = [_row(ctx, r) for r in op.row_ids]
    start = _meta_cost(ctx.run_id)
    extended = build_request(HypothesisMap(rows=rows), ctx.run_id, document_md=ctx.document_md,
                             passport=ctx.passport, existing=ctx.request, trace_prefix=prefix, save=False)
    cost = _meta_cost(ctx.run_id) - start
    new_items = extended.items[len(ctx.request.items):]
    after = "\n".join(f"— {it.metric}" for it in new_items) or "новых позиций нет — всё покрыто существующими"
    return Candidate(op, extended, f"позиций в бланке: {len(ctx.request.items)}", after, cost, cost_known=config.LLM_BACKEND == "claude")


def _accept_request(ctx: _Ctx, cand: Candidate) -> Change:
    extended: DataRequest = cand.payload
    new_items = []
    for it in extended.items[len(ctx.request.items):]:
        new_items.append(it.model_copy(update={"num": str(ctx.state.next_item_num)}))
        ctx.state.next_item_num += 1
    ctx.request = DataRequest(items=ctx.request.items + new_items, years=ctx.request.years,
                              why_dropped=extended.why_dropped)
    for r in cand.op.row_ids:
        ctx.state.marks.stale_rows.pop(r, None)
    return Change("Запрос показателей", f"добавлены позиции № {', '.join(i.num for i in new_items) or '—'} "
                  f"под стр. {', '.join(map(str, cand.op.row_ids))}", None, cand.after)


# ---- реестры и исполнение --------------------------------------------------

_CODE_OPS: dict[str, Callable[[_Ctx, object], Change]] = {
    "правка текста": _edit_text,
    "смена вердикта": _set_verdict,
    "удалить строку карты": _delete_map_row,
    "добавить строку карты": _add_map_row,
    "слить строки карты": _merge_map_rows,
    "пометить: нигде не ведётся": _mark_not_tracked,
    "удалить позицию запроса": _delete_request_item,
    "разделить позицию запроса": _split_request_item,
}
_MODEL_OPS: dict[str, tuple[Callable, Callable]] = {
    "перепроверить индикатор": (_prepare_recheck, _accept_recheck),
    "построить гипотезы": (_prepare_hypotheses, _accept_hypotheses),
    "дополнить запрос": (_prepare_request, _accept_request),
}
_JOURNAL_KIND = {"правка текста": "правка", "перепроверить индикатор": "перепроверка",
                 "построить гипотезы": "досчёт", "дополнить запрос": "досчёт", "откат": "откат"}


@dataclass
class _Rollback:
    """Откат не операция модели — кнопка страницы; для журнала нужны только вид и причина."""

    reason: str
    kind: str = "откат"


def is_model_op(op: Operation) -> bool:
    return op.kind in _MODEL_OPS


def run_step(run_id: str, op: Operation) -> StepResult:
    """Выполнить одну операцию плана: без модели — новая версия; с моделью — кандидат."""
    versions.recover(run_id)
    ctx = _load(run_id, versions.ensure_v0(run_id))
    if op.kind in _MODEL_OPS:
        cand = _MODEL_OPS[op.kind][0](ctx, op, f"dialog/v{ctx.state.current + 1}/")
        return StepResult(version=None, candidate=cand, cost_usd=cand.cost_usd, cost_known=cand.cost_known)
    change = _CODE_OPS[op.kind](ctx, op)
    return _commit(ctx, op, change, 0.0)


def accept(run_id: str, cand: Candidate) -> StepResult:
    """«Принять» кандидата операции с моделью — записать его и зафиксировать версию."""
    ctx = _load(run_id, versions.ensure_v0(run_id))
    change = _MODEL_OPS[cand.op.kind][1](ctx, cand)
    return _commit(ctx, cand.op, change, cand.cost_usd, cand.cost_known)


def rollback(run_id: str, n: int) -> StepResult:
    """
    Откат — новая версия с содержимым версии n: ничего не стирается, в журнале строка «откат».
    Пометки — той версии; счётчики номеров не возвращаются, номера не переиспользуются.
    """
    versions.recover(run_id)
    state = versions.ensure_v0(run_id)
    if not 0 <= n < state.current:
        raise OperationError(f"вернуться можно к версиям 0–{state.current - 1}, а не к {n}")
    current = state.current
    versions.restore(run_id, n)
    ctx = _load(run_id, state)
    old = versions.version_state(run_id, n)
    if old is not None:
        ctx.state.marks = old.marks.model_copy(deep=True)
    change = Change("Весь файл", f"возврат к версии {n}", f"версия {current}", f"как в версии {n}")
    return _commit(ctx, _Rollback(reason=f"возврат к версии {n}"), change, 0.0)


def _commit(ctx: _Ctx, op: Operation, change: Change, cost: float, cost_known: bool = True) -> StepResult:
    run_id, state = ctx.run_id, ctx.state
    n = state.current + 1
    entry = JournalEntry(version=n, at=datetime.datetime.now().isoformat(timespec="seconds"),
                         kind=_JOURNAL_KIND.get(op.kind, "решение эксперта"), target=change.target,
                         action=change.action, before=_short(change.before), after=_short(change.after),
                         reason=op.reason, cost_usd=round(cost, 4), cost_known=cost_known)
    try:
        for v in ctx.verdicts.values():
            save_json(run_id, f"{VERDICTS_DIR}/{v.indicator:02d}.json", v)
        buckets = sort_verdicts(list(ctx.verdicts.values()), run_id)
        for n_ind, hset in ctx.new_sets.items():
            save_json(run_id, f"{HYPOTHESES_DIR}/{n_ind:02d}.json", hset)
        save_json(run_id, MAP_FILE, ctx.hyp_map)
        save_json(run_id, REQUEST_FILE, ctx.request)
        # Новая строка «проверка не завершена» получает номер из того же счётчика, что
        # и строки карты: номер удалённой строки не достаётся никому (шаг 1.5)
        for v in unverified_verdicts(ctx.hyp_map, buckets):
            if v.indicator not in state.unverified_ids:
                state.unverified_ids[v.indicator] = _new_row_id(ctx)
        meta = load_json(run_id, META_FILE, RunMeta)
        analyzed = datetime.datetime.fromisoformat(meta.finished_at) if meta.finished_at else None
        journal = versions.read_journal(run_id) + [entry]
        excel = build_excel(ctx.hyp_map, ctx.request, buckets, ctx.passport, run_id, version=n,
                            journal=journal, marks=state.marks,
                            unverified_ids=state.unverified_ids, analyzed_at=analyzed,
                            act_types=meta.act_types, run_gaps=_run_gaps(run_id, journal))
        versions.commit(run_id, state, [entry])
        _refresh_progress(run_id, ctx, excel)
    except Exception:
        versions.restore(run_id, state.current)
        raise
    return StepResult(version=n, change=change, cost_usd=cost, cost_known=cost_known)


def _run_gaps(run_id: str, journal: list[JournalEntry]) -> set[int] | None:
    """
    «Нарушен» без строки карты на момент разбора — по снимку версии v0 (15.10, код-ревью): это сбой узла
    гипотез, он остаётся на листе «Ошибки прогона»; то, что эксперт сделал сам, — нет. Эксперт построил гипотезы
    по такому индикатору (журнал: «Карта гипотез, инд. N») — сбой закрыт, дальше строки — его решение
    (повторное ревью). Снимка нет — None.
    """
    v0 = f"{VERSIONS_DIR}/v0"
    try:
        gaps = violated_without_rows(load_json(run_id, f"{v0}/{MAP_FILE}", HypothesisMap),
                                     load_json(run_id, f"{v0}/{BUCKETS_FILE}", Buckets))
    except (OSError, ValueError):
        return None
    built = {int(m.group(1)) for e in journal if (m := _BUILT_TARGET.fullmatch(e.target))}
    return gaps - built


def _short(text: str | None, limit: int = 1500) -> str | None:
    return text if text is None or len(text) <= limit else text[:limit] + "…"


def _refresh_progress(run_id: str, ctx: _Ctx, excel) -> None:
    """Сводка страницы прогона — по текущей версии; сам progress.json диалог не создаёт."""
    from app.pipeline import build_summary   # тяжёлый модуль — только когда нужен

    if not (run_dir(run_id) / PROGRESS_FILE).is_file():
        return
    progress = load_json(run_id, PROGRESS_FILE, RunProgress)
    summary = build_summary(list(ctx.verdicts.values()), ctx.hyp_map, ctx.request, excel)
    save_json(run_id, PROGRESS_FILE, progress.model_copy(update={
        "summary": summary, "total_cost_usd": load_json(run_id, META_FILE, RunMeta).cost_usd,
        "cost_known": load_json(run_id, META_FILE, RunMeta).cost_known}))
