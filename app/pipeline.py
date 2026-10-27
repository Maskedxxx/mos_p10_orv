"""
Оркестратор сервиса П-10 — связывает девять готовых узлов в один вызов.

Бизнес-логики здесь нет вовсе: только порядок вызовов, два барьера, учёт
состояния и правила устойчивости. Всё, что узел умеет считать, он считает сам.

Порядок:

    [1]   конвертация docx -> md
    [1.1] саммари
    [2]   12 вердиктов параллельно  ──── барьер (внутри узла)
    [3]   разбор корзины на 4 кучи
    [4а]  гипотезы из нарушений  ┐
    [4б]  гипотезы из пробелов   ┘ одновременно ──── барьер (здесь)
    [5]   сборка карты, слияние дублей
    [6]   запрос показателей
    [7]   итоговый Excel

Саммари передаётся ТОЛЬКО в узел [7] (шапка листа карты) — агентам вердиктов
оно не подаётся, так решено владельцем (см. docstring app/nodes/verdicts.py).

ПРОГРЕСС. Веб-страница рисует граф выполнения, поэтому состояние пишется на
диск при КАЖДОМ его изменении — в отдельный файл progress.json в папке прогона
(модель RunProgress). Отдельно от meta.json намеренно: meta.json — метаданные
прогона, progress.json — состояние выполнения, у файлов разная ответственность
и разная частота записи. Три места, откуда состояние меняется, живут в разных
потоках (колбэк готовности индикатора из пула узла [2], две ветки гипотез,
основной поток), поэтому у _Progress свой лок.

СТОИМОСТЬ. Оркестратор НЕ считает стоимость сам — узлы уже прибавляют её в
meta.json под локом (add_cost в app/trace.py). Здесь только
читается уже посчитанная сумма — под ТЕМ ЖЕ локом (META_LOCK), второй механизм
учёта не заводится. Стоимость узла = разница "после минус до", общая сумма =
значение из meta.json на момент окончания.

УСТОЙЧИВОСТЬ. Падение узла роняет прогон только там, где дальше идти не с чем:

    [1]   конвертация — прогон останавливается (текста акта нет);
    [1.1] саммари — продолжаем, шапка Excel будет без реквизитов;
    [2]   вердикты — сбой отдельных индикаторов узел сам отдаёт вердиктом
          "ошибка" (они попадут на лист «Ошибки прогона»); падение узла целиком
          — продолжаем с пустым списком вердиктов;
    [3]   разбор корзины — продолжаем с пустыми кучами;
    [4]   гипотезы, [5] карта, [6] запрос — узлы сами возвращают безопасный
          результат, продолжаем;
    [7]   Excel — прогон помечается сбоем, но все промежуточные артефакты
          остаются на диске (их писали сами узлы по ходу).

Любой сбой пишется и в progress.json (статус "сбой" + текст причины), и в
run.log. При обрыве процесса на диске остаётся всё, что успело посчитаться:
артефакты пишут узлы по ходу, progress.json — оркестратор по ходу.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.config import all_indicators
from app.contracts import (
    Buckets,
    DataRequest,
    HypothesisMap,
    HypothesisSet,
    NodeState,
    NodeStatus,
    RunMeta,
    RunProgress,
    RunResult,
    RunStatus,
    RunSummary,
    Verdict,
)
from app.logger import get_run_logger
from app.config import PASSPORT_TIMEOUT_S
from app.nodes.convert import convert_to_markdown
from app.nodes.excel import build_excel
from app.nodes.hypotheses import (
    run_hypotheses_no_data,
    run_hypotheses_violated,
)
from app.nodes.merge import merge_map
from app.nodes.request import build_request
from app.nodes.sort import sort_verdicts
from app.nodes.passport import apply_user_developers, make_passport, passport_effective_year
from app.nodes.verdicts import run_verdicts
from app.trace import (
    BUCKETS_FILE, DOCUMENT_FILE, EXCEL_FILE, HYPOTHESES_DIR, INPUT_STEM, MAP_FILE, META_FILE, META_LOCK, PASSPORT_FILE,
    PROGRESS_FILE, REQUEST_FILE, VERDICTS_DIR, load_json, log_event, new_run, save_json, update_meta,
)

# Узлы графа выполнения: ключ (для страницы и для кода) -> человеческое имя.
# Обе ветки гипотез [4а]/[4б] — один узел "hypotheses": их отдельные статусы
# лежат в RunProgress.hypotheses_branches.
_NODES: list[tuple[str, str]] = [
    ("convert", "Конвертация акта в markdown"),
    ("passport", "Паспорт акта"),
    ("verdicts", "Двенадцать индикаторов"),
    ("sort", "Разбор корзины вердиктов"),
    ("hypotheses", "Гипотезы: нарушения и пробелы"),
    ("merge", "Сборка карты гипотез"),
    ("request", "Запрос показателей"),
    ("excel", "Итоговый Excel"),
]


# Граф узлов для журнала сессии (2.7, мониторинг): что узел берёт на вход и что пишет — по коду
# run_pipeline ниже. «00_input» — загруженный акт, meta.json — настройки прогона; папка со слешем
# в конце — все файлы в ней. Вход каждого узла — выход более раннего (проверяет тест журнала).
NODE_GRAPH: dict[str, tuple[list[str], list[str]]] = {
    "convert": ([INPUT_STEM], [DOCUMENT_FILE]),
    "passport": ([DOCUMENT_FILE, META_FILE], [PASSPORT_FILE]),
    "verdicts": ([DOCUMENT_FILE, PASSPORT_FILE, META_FILE], [f"{VERDICTS_DIR}/"]),
    "sort": ([f"{VERDICTS_DIR}/"], [BUCKETS_FILE]),
    "hypotheses": ([BUCKETS_FILE, DOCUMENT_FILE, PASSPORT_FILE], [f"{HYPOTHESES_DIR}/"]),
    "merge": ([f"{HYPOTHESES_DIR}/", DOCUMENT_FILE], [MAP_FILE]),
    "request": ([MAP_FILE, DOCUMENT_FILE, PASSPORT_FILE, META_FILE], [REQUEST_FILE]),
    "excel": ([MAP_FILE, REQUEST_FILE, BUCKETS_FILE, PASSPORT_FILE, META_FILE],
              [EXCEL_FILE]),
}


def _now() -> str:
    """Отметка времени для progress.json — ISO-8601, как в meta.json."""
    return datetime.now().isoformat()


def _cost_now(run_id: str) -> float:
    """
    Прочитать текущую суммарную стоимость прогона из meta.json.

    Чтение идёт под тем же локом, которым узлы прибавляют туда свою стоимость
    (add_cost в app/trace.py) — иначе можно прочитать
    meta.json в момент чужой записи. Второй механизм учёта не заводится:
    оркестратор только читает.
    """
    with META_LOCK:
        return load_json(run_id, META_FILE, RunMeta).cost_usd


class _Progress:
    """
    Состояние выполнения прогона, зеркалируемое в progress.json.

    Каждое изменение состояния немедленно пишется на диск — страница читает
    файл по ходу прогона. Все публичные методы берут лок: события приходят из
    разных потоков (колбэк готовности индикатора — из пула узла [2], статусы
    веток [4а]/[4б] — из их собственных потоков, статусы узлов — из основного).
    """

    def __init__(self, run_id: str, act_filename: str) -> None:
        self._run_id = run_id
        self._lock = threading.Lock()
        self._logger = get_run_logger(run_id).getChild("pipeline")
        # Точки отсчёта по каждому узлу: монотонное время и стоимость на входе
        self._started_mono: dict[str, float] = {}
        self._cost_base: dict[str, float] = {}

        self._state = RunProgress(
            run_id=run_id,
            act_filename=act_filename,
            status="идёт",
            nodes=[NodeState(key=key, title=title) for key, title in _NODES],
            indicators={str(ind.number): "ожидает" for ind in all_indicators()},
            hypotheses_branches={"violated": "ожидает", "no_data": "ожидает"},
            started_at=_now(),
        )
        with self._lock:
            self._save()

    # ---- внутреннее (вызывается под уже взятым локом) ----

    def _save(self) -> None:
        """Записать текущее состояние в progress.json. Лок должен быть взят."""
        save_json(self._run_id, PROGRESS_FILE, self._state)

    def _find_node(self, key: str) -> NodeState:
        """Найти узел по ключу. Лок должен быть взят."""
        for node in self._state.nodes:
            if node.key == key:
                return node
        raise KeyError(f"неизвестный узел прогресса: {key}")

    # ---- узлы ----

    def node_started(self, key: str) -> None:
        """Пометить узел идущим и запомнить точки отсчёта времени и стоимости."""
        with self._lock:
            node = self._find_node(key)
            node.status = "идёт"
            node.started_at = _now()
            self._started_mono[key] = time.monotonic()
            self._cost_base[key] = _cost_now(self._run_id)
            self._save()
        self._logger.info("узел %s: старт", key)
        log_event(self._run_id, "node_started", who=f"узел {key}", node=key, inputs=NODE_GRAPH[key][0])

    def node_finished(self, key: str) -> None:
        """Пометить узел готовым, посчитать длительность и его вклад в стоимость."""
        self._close_node(key, status="готов", error=None)
        self._logger.info("узел %s: готов", key)

    def node_failed(self, key: str, error: str) -> None:
        """Пометить узел сбойным с текстом причины (он же уходит в run.log)."""
        self._close_node(key, status="сбой", error=error)
        self._logger.error("узел %s: сбой — %s", key, error)

    def _close_node(self, key: str, status: NodeStatus, error: str | None) -> None:
        """Общее закрытие узла: время, стоимость, статус, запись на диск."""
        with self._lock:
            node = self._find_node(key)
            total_cost = _cost_now(self._run_id)
            node.status = status
            node.error = error
            node.finished_at = _now()
            node.duration_s = round(time.monotonic() - self._started_mono[key], 3)
            # Вклад узла — разница по meta.json: прибавляют туда сами узлы
            node.cost_usd = round(total_cost - self._cost_base[key], 6)
            self._state.total_cost_usd = round(total_cost, 6)
            self._save()
        if status == "готов":
            log_event(self._run_id, "node_finished", who=f"узел {key}", node=key, outputs=NODE_GRAPH[key][1],
                      duration_s=node.duration_s, cost_usd=node.cost_usd)
        else:
            log_event(self._run_id, "node_failed", who=f"узел {key}", node=key, error=error,
                      duration_s=node.duration_s, cost_usd=node.cost_usd)

    # ---- индикаторы узла [2] ----

    def indicators_started(self) -> None:
        """
        Пометить все двенадцать индикаторов идущими: узел [2] запускает их
        одновременно (max_workers=12 на 12 индикаторов).
        """
        with self._lock:
            self._state.indicators = {key: "идёт" for key in self._state.indicators}
            self._save()

    def indicator_done(self, verdict: Verdict) -> None:
        """
        Колбэк готовности одного индикатора — приходит из потока узла [2] по
        мере готовности, до барьера. Вердикт "ошибка" — это сбой индикатора.
        """
        with self._lock:
            self._state.indicators[str(verdict.indicator)] = (
                "сбой" if verdict.verdict == "ошибка" else "готов"
            )
            self._save()

    def indicators_aborted(self) -> None:
        """
        Пометить сбойными индикаторы, оставшиеся идущими: узел [2] упал целиком,
        и без этого страница показывала бы вечно крутящиеся индикаторы.
        """
        with self._lock:
            for key, status in self._state.indicators.items():
                if status == "идёт":
                    self._state.indicators[key] = "сбой"
            self._save()

    # ---- ветки гипотез [4а]/[4б] ----

    def branch_status(self, branch: str, status: NodeStatus) -> None:
        """Статус одной ветки гипотез — страница показывает, какая уже отработала."""
        with self._lock:
            self._state.hypotheses_branches[branch] = status
            self._save()

    # ---- завершение прогона ----

    def finish(self, status: RunStatus, summary: RunSummary) -> None:
        """Записать финальное состояние прогона: статус, время, сводку, стоимость."""
        with self._lock:
            self._state.status = status
            self._state.finished_at = _now()
            self._state.summary = summary
            self._state.total_cost_usd = round(_cost_now(self._run_id), 6)
            self._save()
        log_event(self._run_id, "run_finished", status=status, cost_usd=self._state.total_cost_usd,
                  summary=summary.model_dump(mode="json"))

    @property
    def total_cost_usd(self) -> float:
        """Общая стоимость прогона на текущий момент."""
        with self._lock:
            return self._state.total_cost_usd


@contextmanager
def _node(progress: _Progress, key: str) -> Iterator[None]:
    """
    Обёртка узла: "идёт" на входе, "готов" на выходе, "сбой" при исключении.

    Исключение пробрасывается дальше — решение "остановиться или идти дальше"
    принимает вызывающий код по правилам устойчивости (см. docstring модуля).
    """
    progress.node_started(key)
    try:
        yield
    except Exception as exc:
        progress.node_failed(key, f"{type(exc).__name__}: {exc}")
        raise
    progress.node_finished(key)


def _run_one_branch(
    progress: _Progress,
    branch: str,
    buckets: Buckets,
    document_md: str,
    passport,
    run_id: str,
    timeout_s: int,
    max_budget_usd: float | None,
) -> tuple[list[HypothesisSet], str | None]:
    """
    Одна ветка гипотез в своём потоке. Наружу не бросает: сбой ветки — это её
    статус "сбой" в progress.json и текст причины, а не падение прогона.

    Возвращает (наборы гипотез, причина сбоя или None).
    """
    node_fn = run_hypotheses_violated if branch == "violated" else run_hypotheses_no_data
    progress.branch_status(branch, "идёт")
    try:
        sets = node_fn(
            buckets, document_md, run_id,
            passport=passport, timeout_s=timeout_s, max_budget_usd=max_budget_usd,
        )
    except Exception as exc:
        progress.branch_status(branch, "сбой")
        return [], f"ветка {branch}: {type(exc).__name__}: {exc}"
    progress.branch_status(branch, "готов")
    return sets, None


def _run_hypotheses_branches(
    progress: _Progress,
    buckets: Buckets,
    document_md: str,
    passport,
    run_id: str,
    timeout_s: int,
    max_budget_usd: float | None,
) -> tuple[list[HypothesisSet], list[str]]:
    """
    Узлы [4а] и [4б] ОДНОВРЕМЕННО, барьер после обеих: выход из блока
    `with ThreadPoolExecutor` дожидается завершения обеих задач.

    Возвращает (объединённые наборы гипотез, список причин сбоя веток).
    Порядок наборов — сначала нарушения, потом пробелы.
    """
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            branch: executor.submit(
                _run_one_branch, progress, branch, buckets, document_md, passport, run_id,
                timeout_s, max_budget_usd,
            )
            for branch in ("violated", "no_data")
        }
        violated_sets, violated_error = futures["violated"].result()
        no_data_sets, no_data_error = futures["no_data"].result()

    errors = [text for text in (violated_error, no_data_error) if text is not None]
    return violated_sets + no_data_sets, errors


def build_summary(
    verdicts: list[Verdict],
    hypothesis_map: HypothesisMap,
    request: DataRequest,
    excel_path: Path | None,
) -> RunSummary:
    """Собрать итоговую сводку прогона из уже посчитанных результатов узлов."""
    counts = Counter(verdict.verdict for verdict in verdicts)
    return RunSummary(
        violated=counts.get("нарушен", 0),
        compliant=counts.get("соблюдён", 0),
        not_applicable=counts.get("не применимо", 0),
        no_data=counts.get("нет данных", 0),
        errors=counts.get("ошибка", 0),
        map_rows=len(hypothesis_map.rows),
        merges=len(hypothesis_map.merges),
        request_items=len(request.items),
        excel_path=str(excel_path) if excel_path is not None else None,
    )


def _finish(
    progress: _Progress,
    run_id: str,
    status: RunStatus,
    summary: RunSummary,
    error: str | None = None,
) -> RunResult:
    """
    Завершить прогон: финальная запись в progress.json и в meta.json.

    meta.json обновляется под META_LOCK — тем же, под которым узлы прибавляют
    туда стоимость (update_meta читает и пишет файл целиком). Статусы meta.json
    остаются в стиле уже записанного туда trace.new_run() "started".
    """
    progress.finish(status, summary)
    with META_LOCK:
        update_meta(
            run_id,
            status="finished" if status == "готов" else "failed",
            finished_at=_now(),
        )
    return RunResult(
        run_id=run_id,
        status=status,
        excel_path=summary.excel_path,
        summary=summary,
        total_cost_usd=progress.total_cost_usd,
        error=error,
    )


def _run_settings(run_id: str) -> RunMeta:
    """
    Выбор пользователя при загрузке (2.7): тип акта, регионы сравнения, потолки поиска.
    Нет meta.json или полей (прогон до 2.7) — значения по умолчанию.
    """
    try:
        return load_json(run_id, META_FILE, RunMeta)
    except Exception:
        return RunMeta(run_id=run_id, act_filename="—", started_at="—", status="—")


def run_pipeline(
    docx_path: str | Path,
    run_id: str | None = None,
    *,
    timeout_s: int = 900,
    max_budget_usd: float | None = None,
) -> RunResult:
    """
    Прогнать акт через весь пайплайн П-10 и вернуть результат прогона.

    run_id=None — папка прогона создаётся здесь через trace.new_run(); переданный
    run_id используется как есть (папка и meta.json прогона должны уже быть).

    Состояние пишется в progress.json папки прогона при каждом его изменении,
    правила устойчивости и учёт стоимости — см. docstring модуля.
    """
    path = Path(docx_path)
    if run_id is None:
        run_id = new_run(path.name)

    logger = get_run_logger(run_id).getChild("pipeline")
    progress = _Progress(run_id, path.name)
    logger.info("прогон начат: акт=%s run_id=%s", path.name, run_id)

    # [1] конвертация — единственный узел, падение которого останавливает прогон
    try:
        with _node(progress, "convert"):
            document_md = convert_to_markdown(path, run_id)
    except Exception as exc:
        logger.error("прогон остановлен: конвертация не дала текста акта")
        empty = build_summary([], HypothesisMap(), DataRequest(), None)
        return _finish(progress, run_id, "сбой", empty, f"{type(exc).__name__}: {exc}")

    # [1.1] паспорт акта — без него идти нельзя: ворота «не применимо» всех
    # 12 промптов стоят на его ключах, годы бланка и шапка Excel — тоже из него
    passport = None
    try:
        with _node(progress, "passport"):
            passport = make_passport(
                document_md, run_id,
                timeout_s=PASSPORT_TIMEOUT_S, max_budget_usd=max_budget_usd,
            )
            if passport is None:
                raise RuntimeError("модель не вернула паспорт акта")
            # 2.7 (А): разработчик, выбранный при загрузке, важнее ответа модели
            user_developers = _run_settings(run_id).developers
            if user_developers:
                passport = apply_user_developers(passport, user_developers)
                save_json(run_id, PASSPORT_FILE, passport)
    except Exception as exc:
        empty = build_summary([], HypothesisMap(), DataRequest(), None)
        return _finish(progress, run_id, "сбой", empty, f"паспорт акта: {type(exc).__name__}: {exc}")

    # [2] двенадцать вердиктов параллельно, барьер внутри узла; готовность
    # каждого индикатора приходит колбэком по ходу — для счётчика "7 из 12"
    verdicts: list[Verdict] = []
    settings = _run_settings(run_id)
    try:
        with _node(progress, "verdicts"):
            progress.indicators_started()
            verdicts = run_verdicts(
                document_md, run_id,
                passport=passport,
                regions=settings.comparison_regions,
                search_limits=settings.search_limits,
                timeout_s=timeout_s,
                max_budget_usd=max_budget_usd,
                on_verdict=progress.indicator_done,
            )
    except Exception:
        # Узел упал целиком (сбой отдельных индикаторов сюда не попадает — он
        # приходит вердиктом "ошибка"). Идём дальше с пустым списком вердиктов.
        progress.indicators_aborted()

    # [3] разбор корзины на четыре кучи
    buckets = Buckets()
    try:
        with _node(progress, "sort"):
            buckets = sort_verdicts(verdicts, run_id)
    except Exception:
        pass

    # [4а]+[4б] одновременно, барьер после обеих
    progress.node_started("hypotheses")
    hypothesis_sets, branch_errors = _run_hypotheses_branches(
        progress, buckets, document_md, passport, run_id, timeout_s, max_budget_usd,
    )
    if branch_errors:
        # Ветка упала — узел помечается сбоем, но прогон продолжается:
        # гипотезы уцелевшей ветки есть, дальше идти есть с чем
        progress.node_failed("hypotheses", "; ".join(branch_errors))
    else:
        progress.node_finished("hypotheses")

    # [5] сборка карты и слияние дублей
    hypothesis_map = HypothesisMap()
    try:
        with _node(progress, "merge"):
            hypothesis_map = merge_map(
                hypothesis_sets, document_md, run_id,
                timeout_s=timeout_s, max_budget_usd=max_budget_usd,
            )
    except Exception:
        pass

    # [6] запрос показателей
    request = DataRequest()
    try:
        with _node(progress, "request"):
            request = build_request(
                hypothesis_map, run_id,
                document_md=document_md,
                passport=passport,
                effective_year=passport_effective_year(passport),
                timeout_s=timeout_s, max_budget_usd=max_budget_usd,
                act_types=settings.act_types,
            )
    except Exception:
        pass

    # [7] итоговый Excel — единственный узел, чьё падение помечает сбоем весь
    # прогон; промежуточные артефакты при этом остаются на диске
    try:
        with _node(progress, "excel"):
            excel_path = build_excel(hypothesis_map, request, buckets, passport, run_id,
                                     act_types=settings.act_types)
    except Exception as exc:
        summary = build_summary(verdicts, hypothesis_map, request, None)
        return _finish(progress, run_id, "сбой", summary, f"{type(exc).__name__}: {exc}")

    summary = build_summary(verdicts, hypothesis_map, request, excel_path)
    logger.info(
        "прогон завершён: нарушено=%d соблюдено=%d не_применимо=%d нет_данных=%d "
        "ошибок=%d строк=%d слияний=%d показателей=%d стоимость=$%.4f",
        summary.violated, summary.compliant, summary.not_applicable, summary.no_data,
        summary.errors, summary.map_rows, summary.merges, summary.request_items,
        progress.total_cost_usd,
    )
    return _finish(progress, run_id, "готов", summary)
