"""
Тесты оркестратора пайплайна П-10 (app/pipeline.py).

Все девять узлов подменены заглушками: оркестратор не должен звать модель.
Заглушки пишут события в общий журнал (с монотонными отметками времени),
кладут на диск настоящие артефакты своих узлов и прибавляют свою стоимость
в meta.json тем же механизмом, что и настоящие узлы (add_cost) —
иначе не проверить ни порядок вызовов, ни сохранность артефактов, ни учёт
стоимости.

Проверяется: порядок вызовов, одновременность веток [4а]/[4б], оба барьера,
запись progress.json ПО ХОДУ прогона, события готовности индикаторов,
правила устойчивости (падение конвертации / саммари / Excel), учёт стоимости
и итоговая сводка.
"""

from __future__ import annotations

import threading
import time

import pytest

import app.pipeline as pipeline
from app.contracts import (
    ActPassport,
    Buckets,
    DataItem,
    DataRequest,
    Effect,
    Hypothesis,
    HypothesisMap,
    HypothesisSet,
    MapRow,
    Merge,
    RequestItem,
    RunProgress,
    Verdict,
    SourceRef,
)
from app.trace import add_cost
from app.pipeline import run_pipeline
from app.trace import load_json, new_run, read_events, run_dir, save_json, save_text


def _minimal_passport_dict() -> dict:
    """Минимальный валидный паспорт (все объекты «нет» с обоснованием) — для заглушек."""
    no = {"state": "нет", "justification": "искал по всему тексту, не установлено"}
    return {
        "requisites": {"kind": "приказ", "number": "N 1", "date": "1 января 2024 г.",
                       "authority": "Департамент", "title": "Об утверждении порядка"},
        "status": {"status": "принят", "status_basis": "есть номер, дата принятия и подпись должностного лица",
                   "effective_date": "21.08.2025", "effective_date_clause": None, "deferred": [], "repealed_clauses": []},
        "addressee": {"scope": "внешним лицам", "categories": [{"name": "перевозчики", "clause": "п. 1"}], "distinctions": []},
        "nature": {"actions": [{"action": "утверждает новое", "target": "Порядок", "clauses": ["п. 1"]}],
                   "issued_under": [], "input_kind": "акт изменений не вносит"},
        "requirements": {**no, "groups": []},
        "procedure": {**no, "participants": None, "result": None, "has_document_check": None, "refusal_grounds": None, "clauses": []},
        "documents": {**no, "items": [], "transfer_clauses": []},
        "deadlines": {**no, "items": []},
        "references": {**no, "acts": [], "authorities": []},
        "allocation": {"competitive": {**no, "kind": None, "resource": None, "clauses": []}, "appointed": {**no, "items": []}},
        "control": {**no, "authority": None, "subject": None, "measures": [], "outcomes": [], "clauses": []},
    }

# Плановые вердикты заглушки узла [2]: три нарушения, два пробела, пять
# соблюдений, одно "не применимо" и одна ошибка — по ним считается сводка
_VERDICT_PLAN: list[tuple[int, str]] = [
    (1, "нарушен"), (2, "нарушен"), (3, "нарушен"),
    (4, "нет данных"), (5, "нет данных"),
    (6, "соблюдён"), (7, "соблюдён"), (8, "соблюдён"), (9, "соблюдён"), (10, "соблюдён"),
    (11, "не применимо"),
    (12, "ошибка"),
]

# Стоимость, которую заглушка каждого узла прибавляет в meta.json
_COSTS: dict[str, float] = {
    "passport": 0.5,
    "verdicts": 2.0,
    "violated": 1.0,
    "no_data": 0.25,
    "merge": 0.75,
    "request": 0.125,
}


def _make_verdict(n: int, value: str) -> Verdict:
    """Минимально валидный вердикт нужного значения."""
    fields: dict = {
        "indicator": n,
        "name": f"индикатор {n}",
        "verdict": value,
        "rationale": "обоснование заглушки",
    }
    if value == "нет данных":
        fields["missing_data"] = "нужны данные по обращениям"
    return Verdict(**fields)


def _make_hypothesis_set(n: int) -> HypothesisSet:
    """Минимально валидный набор гипотез по одному индикатору."""
    return HypothesisSet(
        indicator=n,
        name=f"индикатор {n}",
        violation_kind="издержки",
        why_this_many="одна гипотеза",
        hypotheses=[
            Hypothesis(
                hypothesis=f"гипотеза по индикатору {n}",
                grounds="основание из карточки",
                effects=[Effect(affected="бизнес", metric="число обращений", how_to_calculate="сумма за год", expected_trend="рост показателя относительно года до вступления акта подтвердит гипотезу")],
                data=[DataItem(metric="число заявок", source="запрос органу")],
            )
        ],
    )


def _read_progress(run_id: str) -> RunProgress:
    """Прочитать progress.json прогона с диска."""
    return load_json(run_id, "progress.json", RunProgress)


class Fakes:
    """
    Заглушки девяти узлов + журнал событий прогона.

    fail — множество имён узлов, которые должны упасть;
    delays — искусственные задержки узлов (для проверки одновременности);
    events — журнал в порядке наступления, stamps — монотонное время события.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.fail: set[str] = set()
        self.delays: dict[str, float] = {}
        self.events: list[str] = []
        self.stamps: dict[str, float] = {}
        self._lock = threading.Lock()

        # Что заглушки увидели/записали — для проверок в тестах
        self.passport_seen: dict[str, object] = {}   # узел -> что ему пришло как паспорт
        self.sets_seen: list[HypothesisSet] = []
        self.indicators_ready_after_each: list[int] = []
        self.progress_at_sort: RunProgress | None = None
        self.progress_at_merge: RunProgress | None = None

    # ---- служебное ----

    def _record(self, event: str) -> None:
        with self._lock:
            self.events.append(event)
            self.stamps.setdefault(event, time.monotonic())

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"заглушка узла {name} упала")

    def _pay(self, run_id: str, name: str) -> None:
        """Прибавить стоимость узла тем же механизмом, что и настоящие узлы."""
        cost = _COSTS.get(name, 0.0)
        if cost:
            add_cost(run_id, cost)

    def _sleep(self, name: str) -> None:
        delay = self.delays.get(name, 0.0)
        if delay:
            time.sleep(delay)

    def index(self, event: str) -> int:
        """Порядковый номер события в журнале."""
        return self.events.index(event)

    # ---- заглушки узлов ----

    def convert(self, docx_path, run_id: str) -> str:
        self._record("convert:start")
        self._maybe_fail("convert")
        save_text(run_id, "01_document.md", "текст акта")
        self._record("convert:end")
        return "текст акта"

    def passport(self, document_md: str, run_id: str, **kwargs) -> ActPassport | None:
        self._record("passport:start")
        self._maybe_fail("passport")
        passport = ActPassport(**_minimal_passport_dict())
        save_json(run_id, "02_passport.json", passport)
        self._pay(run_id, "passport")
        self._record("passport:end")
        return passport

    def verdicts(
        self,
        document_md: str,
        run_id: str,
        *,
        indicators=None,
        max_workers: int = 12,
        timeout_s: int = 900,
        max_budget_usd=None,
        on_verdict=None,
        passport=None,
        regions=None,
        search_limits=None,
    ) -> list[Verdict]:
        self._record("verdicts:start")
        self.passport_seen["verdicts"] = passport
        self.passport_seen["verdicts_settings"] = (regions, search_limits)
        self._maybe_fail("verdicts")
        result: list[Verdict] = []
        for n, value in _VERDICT_PLAN:
            self._sleep("indicator")
            verdict = _make_verdict(n, value)
            save_json(run_id, f"03_verdicts/{n:02d}.json", verdict)
            result.append(verdict)
            self._record(f"indicator:{n}")
            if on_verdict is not None:
                on_verdict(verdict)
                # Сколько индикаторов уже отмечены готовыми/сбойными НА ДИСКЕ
                # сразу после колбэка — доказательство, что статусы меняются
                # постепенно, а не все сразу в конце узла
                statuses = _read_progress(run_id).indicators.values()
                done = sum(1 for status in statuses if status in ("готов", "сбой"))
                self.indicators_ready_after_each.append(done)
        self._pay(run_id, "verdicts")
        self._record("verdicts:end")
        return result

    def sort(self, verdicts: list[Verdict], run_id: str) -> Buckets:
        self._record("sort:start")
        self._maybe_fail("sort")
        self.progress_at_sort = _read_progress(run_id)
        buckets = Buckets(
            violated=[v for v in verdicts if v.verdict == "нарушен"],
            no_data=[v for v in verdicts if v.verdict == "нет данных"],
            checked=[v for v in verdicts if v.verdict in ("соблюдён", "не применимо")],
            failed=[v for v in verdicts if v.verdict == "ошибка"],
        )
        save_json(run_id, "04_buckets.json", buckets)
        self._record("sort:end")
        return buckets

    def hypotheses_violated(
        self, buckets: Buckets, document_md: str, run_id: str, **kwargs,
    ) -> list[HypothesisSet]:
        self.passport_seen["violated"] = kwargs.get("passport")
        return self._branch("violated", buckets.violated, run_id)

    def hypotheses_no_data(
        self, buckets: Buckets, document_md: str, run_id: str, **kwargs,
    ) -> list[HypothesisSet]:
        self.passport_seen["no_data"] = kwargs.get("passport")
        return self._branch("no_data", buckets.no_data, run_id)

    def _branch(self, name: str, verdicts: list[Verdict], run_id: str) -> list[HypothesisSet]:
        self._record(f"{name}:start")
        self._sleep(name)
        self._maybe_fail(name)
        sets = [_make_hypothesis_set(v.indicator) for v in verdicts]
        for hset in sets:
            save_json(run_id, f"05_hypotheses/{hset.indicator:02d}.json", hset)
        self._pay(run_id, name)
        self._record(f"{name}:end")
        return sets

    def merge(
        self, hypothesis_sets: list[HypothesisSet], document_md: str, run_id: str, **kwargs,
    ) -> HypothesisMap:
        self._record("merge:start")
        self._maybe_fail("merge")
        self.sets_seen = list(hypothesis_sets)
        self.progress_at_merge = _read_progress(run_id)
        rows = [
            MapRow(
                row_id=i + 1,
                indicators=[hset.indicator],
                hypothesis=hset.hypotheses[0].hypothesis,
                grounds=hset.hypotheses[0].grounds,
                effects=hset.hypotheses[0].effects,
                data=hset.hypotheses[0].data,
            )
            for i, hset in enumerate(hypothesis_sets)
        ]
        merges = [Merge(merged_row_ids=[1, 2], reason="дубль")] if len(rows) >= 2 else []
        hyp_map = HypothesisMap(rows=rows, merges=merges)
        save_json(run_id, "06_map.json", hyp_map)
        self._pay(run_id, "merge")
        self._record("merge:end")
        return hyp_map

    def request(self, hypothesis_map: HypothesisMap, run_id: str, **kwargs) -> DataRequest:
        self._record("request:start")
        self.passport_seen["request"] = kwargs.get("passport")
        self.passport_seen["request_effective_year"] = kwargs.get("effective_year")
        self.passport_seen["request_document"] = kwargs.get("document_md")
        self._maybe_fail("request")
        items = [
            RequestItem(
                num=str(i + 1), kind="показатель по годам", metric=f"показатель {i + 1}", unit="ед.",
                for_rows=[row.row_id], sources=[SourceRef(row_id=row.row_id, metric=row.data[0].metric)],
            )
            for i, row in enumerate(hypothesis_map.rows)
        ]
        value = DataRequest(items=items, years=[2023, 2024, 2025])
        save_json(run_id, "07_request.json", value)
        self._pay(run_id, "request")
        self._record("request:end")
        return value

    def excel(self, hypothesis_map, request, buckets, passport, run_id: str, **kwargs):
        self.passport_seen["excel_act_types"] = kwargs.get("act_types")
        self._record("excel:start")
        self.passport_seen["excel"] = passport
        self._maybe_fail("excel")
        path = run_dir(run_id) / "08_result.xlsx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"xlsx")
        self._record("excel:end")
        return path


@pytest.fixture
def fakes(tmp_runs_dir, monkeypatch) -> Fakes:
    """Подменить все девять узлов заглушками и создать папку прогона."""
    run_id = new_run("приказ.docx")
    f = Fakes(run_id)

    monkeypatch.setattr(pipeline, "convert_to_markdown", f.convert)
    monkeypatch.setattr(pipeline, "make_passport", f.passport)
    monkeypatch.setattr(pipeline, "run_verdicts", f.verdicts)
    monkeypatch.setattr(pipeline, "sort_verdicts", f.sort)
    monkeypatch.setattr(pipeline, "run_hypotheses_violated", f.hypotheses_violated)
    monkeypatch.setattr(pipeline, "run_hypotheses_no_data", f.hypotheses_no_data)
    monkeypatch.setattr(pipeline, "merge_map", f.merge)
    monkeypatch.setattr(pipeline, "build_request", f.request)
    monkeypatch.setattr(pipeline, "build_excel", f.excel)
    return f


@pytest.fixture
def act_file(tmp_path):
    """Файл акта на входе (содержимое неважно — узел конвертации подменён)."""
    path = tmp_path / "приказ.docx"
    path.write_bytes(b"docx")
    return path


def _node(progress: RunProgress, key: str):
    """Состояние узла по ключу из progress.json."""
    return next(node for node in progress.nodes if node.key == key)


# ---- 1. Порядок вызовов ----


def test_nodes_called_in_declared_order(fakes, act_file):
    """1. Узлы вызваны ровно в порядке из ТЗ (обе ветки гипотез — один шаг,
    их взаимный порядок недетерминирован и здесь не проверяется)."""
    run_pipeline(act_file, fakes.run_id)

    single = [e for e in fakes.events if e.split(":")[0] in (
        "convert", "passport", "verdicts", "sort", "merge", "request", "excel",
    )]
    assert single == [
        "convert:start", "convert:end",
        "passport:start", "passport:end",
        "verdicts:start", "verdicts:end",
        "sort:start", "sort:end",
        "merge:start", "merge:end",
        "request:start", "request:end",
        "excel:start", "excel:end",
    ]


def test_passport_routed_to_expected_nodes(fakes, act_file):
    """1.1. Паспорт уходит в вердикты, обе ветки гипотез, запрос (контрактом, как и остальным, плюс
    год вступления в силу и текст акта; 27.10 — раньше словарём) и Excel — и НЕ уходит в слияние."""
    run_pipeline(act_file, fakes.run_id)

    seen = fakes.passport_seen
    assert isinstance(seen["verdicts"], ActPassport)
    assert isinstance(seen["violated"], ActPassport)
    assert isinstance(seen["no_data"], ActPassport)
    assert isinstance(seen["excel"], ActPassport)
    assert isinstance(seen["request"], ActPassport) and seen["request"].requisites.authority == "Департамент"
    assert seen["request_effective_year"] == 2025
    assert seen["request_document"] == "текст акта"
    assert "merge" not in seen


# ---- 2. Одновременность веток [4а] и [4б] ----


def test_hypotheses_branches_run_at_the_same_time(fakes, act_file):
    """2. Ветки [4а] и [4б] идут одновременно: их интервалы пересекаются, а
    узел целиком отрабатывает заметно быстрее суммы задержек (0.4+0.4=0.8с)."""
    fakes.delays = {"violated": 0.4, "no_data": 0.4}

    run_pipeline(act_file, fakes.run_id)

    # Пересечение интервалов — прямое доказательство одновременности
    assert fakes.stamps["violated:start"] < fakes.stamps["no_data:end"]
    assert fakes.stamps["no_data:start"] < fakes.stamps["violated:end"]

    duration = _node(_read_progress(fakes.run_id), "hypotheses").duration_s
    assert duration is not None and duration < 0.65


# ---- 3. Барьеры ----


def test_sort_starts_only_after_all_verdicts(fakes, act_file):
    """3а. Барьер узла [2]: разбор корзины не стартует, пока не отработали все
    двенадцать индикаторов — и в журнале событий, и по статусам на диске."""
    run_pipeline(act_file, fakes.run_id)

    sort_at = fakes.index("sort:start")
    assert sort_at > fakes.index("verdicts:end")
    for n, _ in _VERDICT_PLAN:
        assert fakes.index(f"indicator:{n}") < sort_at

    assert fakes.progress_at_sort is not None
    assert all(
        status in ("готов", "сбой")
        for status in fakes.progress_at_sort.indicators.values()
    )


def test_merge_starts_only_after_both_hypotheses_branches(fakes, act_file):
    """3б. Барьер узла [4]: сборка карты не стартует, пока не отработали обе
    ветки гипотез — и по журналу, и по статусам веток на диске."""
    run_pipeline(act_file, fakes.run_id)

    merge_at = fakes.index("merge:start")
    assert merge_at > fakes.index("violated:end")
    assert merge_at > fakes.index("no_data:end")

    assert fakes.progress_at_merge is not None
    assert fakes.progress_at_merge.hypotheses_branches == {
        "violated": "готов", "no_data": "готов",
    }


def test_merge_receives_hypotheses_of_both_branches(fakes, act_file):
    """3в. В карту уходят наборы обеих веток: три нарушения + два пробела."""
    run_pipeline(act_file, fakes.run_id)

    assert [hset.indicator for hset in fakes.sets_seen] == [1, 2, 3, 4, 5]


# ---- 4. progress.json пишется по ходу ----


def test_progress_written_during_run_not_only_at_the_end(fakes, act_file, monkeypatch):
    """4. Состояние пишется по ходу: в истории записей у узла статус "идёт"
    появляется раньше, чем "готов", а сам файл на диске виден узлам прогона."""
    history: list[RunProgress] = []
    real_save_json = pipeline.save_json

    def _spy(run_id: str, relname: str, obj):
        if relname == pipeline.PROGRESS_FILE:
            history.append(RunProgress.model_validate(obj.model_dump(mode="json")))
        return real_save_json(run_id, relname, obj)

    monkeypatch.setattr(pipeline, "save_json", _spy)

    run_pipeline(act_file, fakes.run_id)

    running_at = next(i for i, p in enumerate(history) if _node(p, "verdicts").status == "идёт")
    done_at = next(i for i, p in enumerate(history) if _node(p, "verdicts").status == "готов")
    assert running_at < done_at
    # Последняя запись — не единственная: состояние писалось многократно
    assert len(history) > len(pipeline._NODES)

    # Файл действительно был на диске в середине прогона (прочитан заглушкой [3])
    assert fakes.progress_at_sort is not None
    assert _node(fakes.progress_at_sort, "convert").status == "готов"
    assert _node(fakes.progress_at_sort, "sort").status == "идёт"
    assert _node(fakes.progress_at_sort, "excel").status == "ожидает"


def test_progress_has_all_nodes_ready_at_the_end(fakes, act_file):
    """4.1. По окончании все восемь узлов готовы, прогон "готов", время проставлено."""
    run_pipeline(act_file, fakes.run_id)

    progress = _read_progress(fakes.run_id)
    assert progress.status == "готов"
    assert progress.finished_at is not None
    assert [node.status for node in progress.nodes] == ["готов"] * 8
    assert all(node.duration_s is not None for node in progress.nodes)


# ---- 5. События по индикаторам ----


def test_indicator_statuses_change_one_by_one(fakes, act_file):
    """5. Готовность индикаторов приходит по мере поступления: после каждого
    колбэка на диске ровно на один готовый индикатор больше."""
    run_pipeline(act_file, fakes.run_id)

    assert fakes.indicators_ready_after_each == list(range(1, 13))


def test_failed_indicator_marked_as_failure(fakes, act_file):
    """5.1. Вердикт "ошибка" (индикатор 12) даёт статус "сбой", остальные — "готов"."""
    run_pipeline(act_file, fakes.run_id)

    indicators = _read_progress(fakes.run_id).indicators
    assert indicators["12"] == "сбой"
    assert all(indicators[str(n)] == "готов" for n in range(1, 12))


# ---- 6-8. Устойчивость ----


def test_convert_failure_stops_the_run(fakes, act_file):
    """6. Падение конвертации останавливает прогон: статус "сбой", причина
    записана, следующие узлы не запускались."""
    fakes.fail = {"convert"}

    result = run_pipeline(act_file, fakes.run_id)

    assert result.status == "сбой"
    assert "заглушка узла convert упала" in (result.error or "")
    assert fakes.events == ["convert:start"]

    progress = _read_progress(fakes.run_id)
    assert progress.status == "сбой"
    assert _node(progress, "convert").status == "сбой"
    assert "заглушка узла convert упала" in (_node(progress, "convert").error or "")
    assert _node(progress, "verdicts").status == "ожидает"
    assert result.excel_path is None


def test_passport_failure_stops_the_run(fakes, act_file):
    """7. Падение паспорта останавливает прогон: без паспорта ворота всех 12
    промптов недостоверны. Вердикты не вызываются, Excel не собирается."""
    fakes.fail = {"passport"}

    result = run_pipeline(act_file, fakes.run_id)

    assert result.status == "сбой"
    assert result.excel_path is None
    assert "паспорт акта" in (result.error or "")
    assert "verdicts:start" not in fakes.events

    progress = _read_progress(fakes.run_id)
    assert _node(progress, "passport").status == "сбой"
    assert _node(progress, "verdicts").status == "ожидает"


def test_excel_failure_marks_run_failed_but_keeps_artifacts(fakes, act_file):
    """8. Падение Excel помечает прогон сбоем, но артефакты предыдущих узлов
    остаются на диске."""
    fakes.fail = {"excel"}

    result = run_pipeline(act_file, fakes.run_id)

    assert result.status == "сбой"
    assert result.excel_path is None
    assert "заглушка узла excel упала" in (result.error or "")

    folder = run_dir(fakes.run_id)
    for artefact in (
        "01_document.md", "02_passport.json", "03_verdicts/01.json", "04_buckets.json",
        "05_hypotheses/01.json", "06_map.json", "07_request.json", "progress.json",
    ):
        assert (folder / artefact).exists(), artefact

    progress = _read_progress(fakes.run_id)
    assert _node(progress, "excel").status == "сбой"
    assert _node(progress, "request").status == "готов"


def test_hypotheses_branch_failure_does_not_stop_the_run(fakes, act_file):
    """8.1. Падение одной ветки гипотез: узел помечен сбоем, ветка — "сбой",
    вторая ветка отработала, прогон дошёл до Excel."""
    fakes.fail = {"violated"}

    result = run_pipeline(act_file, fakes.run_id)

    assert result.status == "готов"
    progress = _read_progress(fakes.run_id)
    assert progress.hypotheses_branches == {"violated": "сбой", "no_data": "готов"}
    assert _node(progress, "hypotheses").status == "сбой"
    assert "ветка violated" in (_node(progress, "hypotheses").error or "")
    # В карту ушли только гипотезы уцелевшей ветки
    assert [hset.indicator for hset in fakes.sets_seen] == [4, 5]


def test_verdicts_node_failure_continues_with_empty_result(fakes, act_file):
    """8.2. Падение узла [2] целиком: прогон идёт дальше с пустым списком
    вердиктов, а зависшие индикаторы помечаются сбоем (не крутятся вечно)."""
    fakes.fail = {"verdicts"}

    result = run_pipeline(act_file, fakes.run_id)

    assert result.status == "готов"
    progress = _read_progress(fakes.run_id)
    assert _node(progress, "verdicts").status == "сбой"
    assert set(progress.indicators.values()) == {"сбой"}
    assert result.summary.violated == 0
    assert result.summary.map_rows == 0


# ---- 9. Стоимость ----


def test_cost_is_summed_over_all_nodes(fakes, act_file):
    """9. Общая стоимость равна сумме вкладов узлов, а разбивка по узлам в
    progress.json — вкладу каждого."""
    result = run_pipeline(act_file, fakes.run_id)

    expected_total = sum(_COSTS.values())
    assert result.total_cost_usd == pytest.approx(expected_total)

    progress = _read_progress(fakes.run_id)
    assert progress.total_cost_usd == pytest.approx(expected_total)
    assert _node(progress, "passport").cost_usd == pytest.approx(_COSTS["passport"])
    assert _node(progress, "verdicts").cost_usd == pytest.approx(_COSTS["verdicts"])
    # Узел гипотез — сумма обеих веток, он один на [4а] и [4б]
    assert _node(progress, "hypotheses").cost_usd == pytest.approx(
        _COSTS["violated"] + _COSTS["no_data"]
    )
    assert _node(progress, "merge").cost_usd == pytest.approx(_COSTS["merge"])
    assert _node(progress, "request").cost_usd == pytest.approx(_COSTS["request"])
    # Узлы без вызова модели ничего не стоят
    assert _node(progress, "convert").cost_usd == 0.0
    assert _node(progress, "sort").cost_usd == 0.0
    assert _node(progress, "excel").cost_usd == 0.0


# ---- 10. Итоговая сводка ----


def test_summary_numbers_are_correct(fakes, act_file):
    """10. Сводка заполнена правильными числами и лежит и в результате, и в
    progress.json."""
    result = run_pipeline(act_file, fakes.run_id)

    summary = result.summary
    assert summary.violated == 3
    assert summary.compliant == 5
    assert summary.not_applicable == 1
    assert summary.no_data == 2
    assert summary.errors == 1
    assert summary.map_rows == 5      # 3 нарушения + 2 пробела
    assert summary.merges == 1
    assert summary.request_items == 5
    assert summary.excel_path == str(run_dir(fakes.run_id) / "08_result.xlsx")

    progress = _read_progress(fakes.run_id)
    assert progress.summary is not None
    assert progress.summary.model_dump() == summary.model_dump()
    assert result.excel_path == summary.excel_path


# ---- прочее ----


def test_run_id_created_when_not_given(fakes, act_file):
    """Без run_id прогон создаёт свою папку через trace.new_run и возвращает id."""
    result = run_pipeline(act_file)

    assert result.run_id != fakes.run_id
    assert (run_dir(result.run_id) / "progress.json").exists()
    assert _read_progress(result.run_id).act_filename == "приказ.docx"


def test_meta_finished_at_the_end(fakes, act_file):
    """meta.json по окончании помечен завершённым — статус и время проставлены."""
    from app.contracts import RunMeta

    run_pipeline(act_file, fakes.run_id)

    meta = load_json(fakes.run_id, "meta.json", RunMeta)
    assert meta.status == "finished"
    assert meta.finished_at is not None


def test_run_settings_reach_nodes(fakes, act_file):
    """2.7: тип акта и настройки прогона из meta.json доходят до вердиктов, запроса и Excel."""
    from app.trace import update_meta
    update_meta(fakes.run_id, act_types=["господдержка"], comparison_regions=["Казань"], search_limits={3: 30})
    run_pipeline(act_file, fakes.run_id)
    assert fakes.passport_seen["verdicts_settings"] == (["Казань"], {3: 30})
    assert fakes.passport_seen["excel_act_types"] == ["господдержка"]


def test_user_developer_replaces_model_answer(fakes, act_file):
    """А: орган-разработчик, выбранный при загрузке, — в паспорте для всех следующих шагов."""
    from app.trace import update_meta
    update_meta(fakes.run_id, developers=["Департамент культуры города Москвы"])
    run_pipeline(act_file, fakes.run_id)
    seen = fakes.passport_seen["verdicts"]
    assert [d.name for d in seen.requisites.developers] == ["Департамент культуры города Москвы"]



# ---- Журнал сессии: граф узлов (2.7, мониторинг) ----


def test_events_journal_records_node_graph(fakes, act_file):
    """Каждый узел — старт с входами и конец с выходами, длительностью и стоимостью; вход узла — выход
    более раннего узла (или загруженный акт и настройки прогона), так что граф читается из журнала целиком."""
    run_pipeline(act_file, fakes.run_id)
    ev = read_events(fakes.run_id)
    order = ["convert", "passport", "verdicts", "sort", "hypotheses", "merge", "request", "excel"]
    started = [e for e in ev if e["event"] == "node_started"]
    finished = {e["node"]: e for e in ev if e["event"] == "node_finished"}
    assert [e["node"] for e in started] == order and list(finished) == order
    assert all(e["who"] == f"узел {e['node']}" for e in started)
    produced = {"00_input", "meta.json"}
    for e in started:
        assert set(e["inputs"]) <= produced, (e["node"], set(e["inputs"]) - produced)
        produced |= set(finished[e["node"]]["outputs"])
    assert finished["verdicts"]["outputs"] == ["03_verdicts/"] and finished["passport"]["cost_usd"] >= 0
    assert finished["excel"]["duration_s"] >= 0
    done = [e for e in ev if e["event"] == "run_finished"]
    assert len(done) == 1 and done[0]["status"] == "готов" and "cost_usd" in done[0]
    assert any(e["event"] == "artifact" and e["path"] == "03_verdicts/01.json" for e in ev)


def test_events_journal_records_node_failure(fakes, act_file):
    """Сбой узла — событие с текстом причины; прогон идёт дальше, как и раньше."""
    fakes.fail = {"sort"}
    run_pipeline(act_file, fakes.run_id)
    failed = [e for e in read_events(fakes.run_id) if e["event"] == "node_failed"]
    assert [e["node"] for e in failed] == ["sort"] and "заглушка узла sort упала" in failed[0]["error"]
