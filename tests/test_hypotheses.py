"""
Тесты app/nodes/hypotheses.py (узлы [4а]/[4б], параллельный прогон гипотез по куче вердиктов).

runner.call подменяется моком — реально прогоняется только логика узла (сборка
документа с карточкой вердикта, барьер, обработка ошибок, трейсы, суммарная
стоимость с прибавлением к meta.json). Промпты 04a/04b подменяются фикстурными
файлами во временном каталоге — каталог prompts/ проекта не трогаем (там
владелец допишет настоящие промпты позже).
"""

from __future__ import annotations

import re
import threading
import time as time_module
from unittest.mock import Mock

import pytest

import app.nodes.hypotheses as hyp_module
from app.contracts import Buckets, DataItem, Effect, Hypothesis, HypothesisSet, RunMeta, Verdict
from app.nodes.hypotheses import run_hypotheses_no_data, run_hypotheses_violated
from app.runner import CallResult
from app.trace import load_json, run_dir, save_json

_TRACE_N_RE = re.compile(r"(\d{2})\.raw\.json$")


def _n_from_trace_name(trace_name: str) -> int:
    """Достать номер индикатора из имени трейса вида "05_hypotheses/06.raw.json"."""
    match = _TRACE_N_RE.search(trace_name)
    assert match is not None, f"неожиданное имя трейса: {trace_name}"
    return int(match.group(1))


def _verdict(n: int, verdict: str = "нарушен", **extra) -> Verdict:
    """Собрать минимально валидный Verdict с заданным номером и статусом."""
    data = {
        "indicator": n,
        "name": f"индикатор {n}",
        "verdict": verdict,
        "rationale": "проверено по тексту акта",
        **extra,
    }
    if verdict == "нет данных" and "missing_data" not in data:
        data["missing_data"] = "нужен показатель X"
    return Verdict(**data)


def _buckets_violated(nums: list[int]) -> Buckets:
    return Buckets(violated=[_verdict(n, "нарушен") for n in nums], no_data=[], checked=[], failed=[])


def _buckets_no_data(nums: list[int]) -> Buckets:
    return Buckets(
        violated=[], no_data=[_verdict(n, "нет данных") for n in nums], checked=[], failed=[],
    )


def _ok_call_result(n: int, *, attempts: int = 1, cost_usd: float = 0.01) -> CallResult[HypothesisSet]:
    """Успешный CallResult с провалидированным HypothesisSet (одна гипотеза) внутри."""
    hypothesis = Hypothesis(
        hypothesis="требование избыточно",
        affected="субъекты МСП",
        effect=Effect(metric="число обращений", how_to_calculate="сумма за год"),
        data=[DataItem(metric="число заявок", source="открытый")],
    )
    value = HypothesisSet(
        indicator=n, name=f"индикатор {n}", why_this_many="выявлено одно нарушение",
        hypotheses=[hypothesis],
    )
    return CallResult(
        value=value, ok=True, attempts=attempts, cost_usd=cost_usd,
        session_id="s", error=None, permission_denials=[], duration_s=0.1,
    )


def _failed_call_result(
    *, attempts: int = 3, error: str = "процесс упал 3 раза", cost_usd: float = 0.02,
) -> CallResult[HypothesisSet]:
    """Провальный CallResult (ok=False)."""
    return CallResult(
        value=None, ok=False, attempts=attempts, cost_usd=cost_usd,
        session_id=None, error=error, permission_denials=[], duration_s=0.1,
    )


def _default_side_effect(instruction, document, model_cls, **kwargs):
    """Дефолтный мок call(): всем вердиктам — успех с одной гипотезой."""
    n = _n_from_trace_name(kwargs["trace_name"])
    return _ok_call_result(n)


def _side_effect_with_overrides(overrides: dict):
    """Мок call(): для номеров из overrides — заданный CallResult, остальным — успех."""
    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        if n in overrides:
            return overrides[n]
        return _ok_call_result(n)
    return _effect


@pytest.fixture
def tmp_prompts(tmp_path, monkeypatch):
    """
    Подменить PROMPT_HYPOTHESES_VIOLATED/NO_DATA на фиктивные файлы во временном
    каталоге. Патчим имя в самом модуле hypotheses (`hyp_module....`), а не в
    app.config — модуль импортировал константы как значения при импорте, патч
    атрибута config.py на них уже не подействует (тот же приём, что и с call()
    в test_verdicts.py: патчим место использования).
    """
    violated_path = tmp_path / "04a_hypotheses.md"
    violated_path.write_text("Промпт 04a: гипотезы из нарушений (фикстура).", encoding="utf-8")
    no_data_path = tmp_path / "04b_hypotheses.md"
    no_data_path.write_text("Промпт 04b: гипотезы из пробелов в данных (фикстура).", encoding="utf-8")

    monkeypatch.setattr(hyp_module, "PROMPT_HYPOTHESES_VIOLATED", violated_path)
    monkeypatch.setattr(hyp_module, "PROMPT_HYPOTHESES_NO_DATA", no_data_path)
    return violated_path, no_data_path


def _make_run(rid: str, *, cost_usd: float = 0.0) -> None:
    """Создать meta.json прогона с заданной стартовой стоимостью."""
    meta = RunMeta(
        run_id=rid, act_filename="act.docx",
        started_at="2026-01-01T12:00:00", status="started", cost_usd=cost_usd,
    )
    save_json(rid, "meta.json", meta)


@pytest.fixture
def run_id(tmp_runs_dir) -> str:
    """run_id с уже созданным meta.json (cost_usd=0.0) — _add_cost_to_meta его требует."""
    rid = "run_hypotheses_test"
    _make_run(rid)
    return rid


def test_one_call_per_verdict_same_count_results(run_id, tmp_prompts, monkeypatch):
    """1. На каждый вердикт из кучи один вызов модели, результатов столько же."""
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(hyp_module, "call", mock_call)

    result = run_hypotheses_violated(_buckets_violated([1, 5, 9]), "текст", run_id)

    assert len(result) == 3
    assert mock_call.call_count == 3
    assert [s.indicator for s in result] == [1, 5, 9]


def test_real_parallelism_faster_than_sum_of_delays(run_id, tmp_prompts, monkeypatch):
    """2. Параллельность реальная: пять задач с искусственной задержкой 0.3с
    каждая отрабатывают заметно быстрее суммы задержек (1.5с)."""
    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        time_module.sleep(0.3)
        return _ok_call_result(n)

    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_effect))

    started = time_module.monotonic()
    run_hypotheses_violated(_buckets_violated([1, 2, 3, 4, 5]), "текст", run_id, max_workers=5)
    duration = time_module.monotonic() - started

    assert duration < 1.0  # заведомо меньше последовательных 5*0.3=1.5с


def test_one_failure_gives_empty_set_others_intact(run_id, tmp_prompts, monkeypatch):
    """3. Провал одного вызова даёт пустой HypothesisSet с текстом ошибки в
    why_this_many, остальные целы."""
    overrides = {5: _failed_call_result(error="таймаут процесса после 900 с")}
    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_side_effect_with_overrides(overrides)))

    result = run_hypotheses_violated(_buckets_violated([1, 5, 9]), "текст", run_id)
    by_n = {s.indicator: s for s in result}

    assert by_n[5].hypotheses == []
    assert "таймаут процесса" in by_n[5].why_this_many
    assert by_n[1].hypotheses != []
    assert by_n[9].hypotheses != []


def test_failure_does_not_invent_hypothesis(run_id, tmp_prompts, monkeypatch):
    """4. Провал НЕ выдумывает гипотезу — отдельный явный тест."""
    overrides = {1: _failed_call_result()}
    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_side_effect_with_overrides(overrides)))

    result = run_hypotheses_violated(_buckets_violated([1]), "текст", run_id)

    assert result[0].hypotheses == []
    assert result[0].hypotheses is not None


def test_tools_are_empty(run_id, tmp_prompts, monkeypatch):
    """5. Инструменты в вызов уходят пустые."""
    seen_tools: list[list[str]] = []

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        seen_tools.append(kwargs["allowed_tools"])
        return _ok_call_result(n)

    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_effect))

    run_hypotheses_violated(_buckets_violated([1, 5]), "текст", run_id)

    assert len(seen_tools) == 2
    for tools in seen_tools:
        assert tools == []


def test_stdin_has_document_and_distinguishable_verdict_card(run_id, tmp_prompts, monkeypatch):
    """6. В stdin есть и текст акта, и карточка вердикта, и они различимы."""
    seen_documents: dict[int, str] = {}

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        seen_documents[n] = document
        return _ok_call_result(n)

    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_effect))

    run_hypotheses_violated(_buckets_violated([1]), "ПОЛНЫЙ ТЕКСТ МАРКЕР", run_id)

    doc = seen_documents[1]
    assert "## ПОЛНЫЙ ТЕКСТ АКТА" in doc
    assert "ПОЛНЫЙ ТЕКСТ МАРКЕР" in doc
    assert "## КАРТОЧКА ВЕРДИКТА (JSON)" in doc
    assert '"verdict": "нарушен"' in doc
    # Различимы: заголовок текста акта стоит раньше заголовка карточки вердикта,
    # а сам текст акта не встречается внутри JSON-блока карточки
    text_pos = doc.index("## ПОЛНЫЙ ТЕКСТ АКТА")
    card_pos = doc.index("## КАРТОЧКА ВЕРДИКТА (JSON)")
    assert text_pos < card_pos
    assert doc.index("ПОЛНЫЙ ТЕКСТ МАРКЕР") < card_pos


def test_violated_branch_uses_prompt_04a(run_id, tmp_prompts, monkeypatch):
    """7a. Ветка нарушений берёт промпт 04a."""
    seen_instructions: list[str] = []

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        seen_instructions.append(instruction)
        return _ok_call_result(n)

    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_effect))

    run_hypotheses_violated(_buckets_violated([1]), "текст", run_id)

    assert "Промпт 04a" in seen_instructions[0]


def test_no_data_branch_uses_prompt_04b(run_id, tmp_prompts, monkeypatch):
    """7b. Ветка пробелов в данных берёт промпт 04b."""
    seen_instructions: list[str] = []

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        seen_instructions.append(instruction)
        return _ok_call_result(n)

    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_effect))

    run_hypotheses_no_data(_buckets_no_data([3]), "текст", run_id)

    assert "Промпт 04b" in seen_instructions[0]


def test_artifacts_written_for_each(run_id, tmp_prompts, monkeypatch):
    """8. Артефакты 05_hypotheses/NN.json созданы для каждого вердикта кучи."""
    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_default_side_effect))

    run_hypotheses_violated(_buckets_violated([1, 5, 9]), "текст", run_id)

    hyp_dir = run_dir(run_id) / "05_hypotheses"
    for n in (1, 5, 9):
        path = hyp_dir / f"{n:02d}.json"
        assert path.exists(), f"не найден артефакт {path}"


def test_cost_added_not_overwritten(tmp_runs_dir, tmp_prompts, monkeypatch):
    """9. Стоимость ПРИБАВЛЯЕТСЯ к уже записанной в meta.json (узел вердиктов уже
    написал туда свою сумму), а не затирает её."""
    rid = "run_cost_addition_test"
    _make_run(rid, cost_usd=1.5)  # имитация суммы, уже записанной узлом [2]

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        return _ok_call_result(n, cost_usd=0.10)

    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_effect))

    run_hypotheses_violated(_buckets_violated([1, 5, 9]), "текст", rid)

    meta = load_json(rid, "meta.json", RunMeta)
    assert meta.cost_usd == pytest.approx(1.5 + 0.30)  # 1.5 уже было + 3 * 0.10


def test_both_branches_concurrent_dont_lose_cost(tmp_runs_dir, tmp_prompts, monkeypatch):
    """10. Гонка: обе ветки, запущенные одновременно (в разных потоках), не теряют
    стоимость друг друга при записи в meta.json.

    Между чтением текущей стоимости и записью новой искусственно вставлена
    задержка — без блокировки в _add_cost_to_meta этот тест воспроизводимо
    ловил бы потерянное обновление (классический read-modify-write race)."""
    rid = "run_race_test"
    _make_run(rid, cost_usd=1.0)  # имитация суммы, уже записанной узлом [2]

    real_load_json = hyp_module.load_json

    def _slow_load_json(*args, **kwargs):
        result = real_load_json(*args, **kwargs)
        time_module.sleep(0.05)  # расширяет окно гонки между чтением и записью
        return result

    monkeypatch.setattr(hyp_module, "load_json", _slow_load_json)

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        return _ok_call_result(n, cost_usd=0.10)

    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_effect))

    errors: list[Exception] = []

    def _run_violated():
        try:
            run_hypotheses_violated(_buckets_violated([1, 5, 9]), "текст", rid)
        except Exception as exc:  # pragma: no cover - для диагностики упавшего потока
            errors.append(exc)

    def _run_no_data():
        try:
            run_hypotheses_no_data(_buckets_no_data([3, 8]), "текст", rid)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=_run_violated)
    t2 = threading.Thread(target=_run_no_data)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []
    meta = load_json(rid, "meta.json", RunMeta)
    # 1.0 уже было + 3 вердикта violated * 0.10 + 2 вердикта no_data * 0.10
    assert meta.cost_usd == pytest.approx(1.0 + 0.30 + 0.20)
