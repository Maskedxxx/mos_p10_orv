"""
Тесты app/nodes/verdicts.py (узел [2], параллельный прогон 12 индикаторов).

runner.call подменяется моком — реально прогоняется только логика узла (сборка
документа, инструменты по маршруту, барьер, обработка ошибок, attempts, трейсы,
суммарная стоимость). Промпты подменяются фикстурой во временном каталоге —
каталог prompts/ проекта не трогается (там параллельно работает другой агент).

Саммари агентам не подаётся (решение владельца): пруф обязан быть дословной
цитатой из акта, а цитировать из саммари нечего. run_verdicts принимает только
document_md — без параметра саммари.
"""

from __future__ import annotations

import re
import time as time_module
from pathlib import Path
from unittest.mock import Mock

import pytest

import app.config as config_module
import app.nodes.verdicts as verdicts_module
from app.config import get_indicator
from app.contracts import RunMeta, Verdict
from app.nodes.verdicts import run_verdicts
from app.runner import CallResult
from app.trace import load_json, run_dir, save_json

_TRACE_N_RE = re.compile(r"(\d{2})\.raw\.json$")


def _n_from_trace_name(trace_name: str) -> int:
    """Достать номер индикатора из имени трейса вида "03_verdicts/06.raw.json"."""
    match = _TRACE_N_RE.search(trace_name)
    assert match is not None, f"неожиданное имя трейса: {trace_name}"
    return int(match.group(1))


def _ok_call_result(
    n: int,
    *,
    attempts: int = 1,
    cost_usd: float = 0.01,
    model_attempts: int = 1,
    verdict: str = "соблюдён",
) -> CallResult[Verdict]:
    """Успешный CallResult с провалидированным Verdict внутри."""
    indicator = get_indicator(n)
    value = Verdict(
        indicator=n,
        name=indicator.name,
        verdict=verdict,
        rationale="проверено по тексту акта",
        attempts=model_attempts,
    )
    return CallResult(
        value=value, ok=True, attempts=attempts, cost_usd=cost_usd,
        session_id="s", error=None, permission_denials=[], duration_s=0.1,
    )


def _failed_call_result(
    *, attempts: int = 3, error: str = "процесс упал 3 раза", cost_usd: float = 0.02,
) -> CallResult[Verdict]:
    """Провальный CallResult (ok=False)."""
    return CallResult(
        value=None, ok=False, attempts=attempts, cost_usd=cost_usd,
        session_id=None, error=error, permission_denials=[], duration_s=0.1,
    )


def _default_side_effect(instruction, document, model_cls, **kwargs):
    """Дефолтный мок call(): всем индикаторам — успех со статусом "соблюдён"."""
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
def tmp_prompts_dir(tmp_path, monkeypatch):
    """
    Подменить PROMPTS_DIR на временный каталог с фиктивными промптами всех 12
    индикаторов. Каталог prompts/ проекта НЕ трогаем — там параллельно работает
    другой агент, переносящий туда настоящие промпты.
    """
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    for n in range(1, 13):
        (prompts_dir / f"02_indicator_{n:02d}.md").write_text(
            f"Промпт индикатора {n} (фикстура для тестов).", encoding="utf-8",
        )
    monkeypatch.setattr(config_module, "PROMPTS_DIR", prompts_dir)
    return prompts_dir


@pytest.fixture
def run_id(tmp_runs_dir) -> str:
    """run_id с уже созданным meta.json — update_meta() внутри run_verdicts его требует."""
    rid = "run_verdicts_test"
    meta = RunMeta(
        run_id=rid, act_filename="act.docx",
        started_at="2026-01-01T12:00:00", status="started",
    )
    save_json(rid, "meta.json", meta)
    return rid


def test_twelve_verdicts_ordered_by_indicator(run_id, tmp_prompts_dir, monkeypatch):
    """1. Двенадцать вердиктов на выходе при двенадцати индикаторах, порядок по номеру."""
    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_default_side_effect))

    result = run_verdicts("текст акта", run_id)

    assert len(result) == 12
    assert [v.indicator for v in result] == list(range(1, 13))


def test_barrier_returns_exact_count_even_with_slow_tasks(run_id, tmp_prompts_dir, monkeypatch):
    """2. Барьер: результат содержит ровно столько элементов, сколько запрошено,
    даже если часть задач медленнее остальных."""
    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        if n in (2, 9):
            time_module.sleep(0.2)
        return _ok_call_result(n)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    result = run_verdicts("текст", run_id, indicators=[1, 2, 3, 9, 12])

    assert len(result) == 5
    assert [v.indicator for v in result] == [1, 2, 3, 9, 12]


def test_one_agent_failure_does_not_break_others(run_id, tmp_prompts_dir, monkeypatch):
    """3. Провал одного агента не роняет прогон: остальные одиннадцать нормальные,
    у упавшего verdict == "ошибка", attempts из CallResult."""
    overrides = {5: _failed_call_result(attempts=3, error="таймаут процесса после 900 с")}
    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_side_effect_with_overrides(overrides)))

    result = run_verdicts("текст", run_id)
    by_n = {v.indicator: v for v in result}

    assert len(result) == 12
    assert by_n[5].verdict == "ошибка"
    assert by_n[5].attempts == 3
    assert "таймаут процесса" in by_n[5].rationale
    for n in range(1, 13):
        if n != 5:
            assert by_n[n].verdict != "ошибка"


def test_exception_inside_thread_becomes_error_verdict(run_id, tmp_prompts_dir, monkeypatch):
    """4. Исключение внутри потока превращается в "ошибка", а не всплывает наружу."""
    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        if n == 8:
            raise RuntimeError("неожиданный сбой сети")
        return _ok_call_result(n)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    # Не должно бросить исключение наружу
    result = run_verdicts("текст", run_id)
    by_n = {v.indicator: v for v in result}

    assert len(result) == 12
    assert by_n[8].verdict == "ошибка"
    assert "неожиданный сбой сети" in by_n[8].rationale


def test_failure_never_becomes_checked(run_id, tmp_prompts_dir, monkeypatch):
    """5. Провал НЕ становится "соблюдён" — ключевое требование владельца:
    молча проглоченный агент дал бы приказу зелёный свет там, где не проверяли."""
    overrides = {4: _failed_call_result()}
    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_side_effect_with_overrides(overrides)))

    result = run_verdicts("текст", run_id)
    failed = next(v for v in result if v.indicator == 4)

    assert failed.verdict == "ошибка"
    assert failed.verdict != "соблюдён"


def test_tools_follow_route_for_all_twelve_indicators(run_id, tmp_prompts_dir, monkeypatch):
    """6. Инструменты по маршруту: для индикаторов 1,2,5,6 — пустой список,
    для остальных восьми — оба инструмента. Проверка на всех двенадцати."""
    seen_tools: dict[int, list[str]] = {}

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        seen_tools[n] = kwargs["allowed_tools"]
        return _ok_call_result(n)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    run_verdicts("текст", run_id)

    text_route_numbers = {1, 2, 5, 6}
    assert set(seen_tools) == set(range(1, 13))
    for n in range(1, 13):
        if n in text_route_numbers:
            assert seen_tools[n] == []
        else:
            assert seen_tools[n] == ["WebSearch", "WebFetch"]


def test_attempts_overwritten_from_call_result(run_id, tmp_prompts_dir, monkeypatch):
    """7. attempts перезаписывается значением из CallResult, даже если модель
    вернула другое."""
    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        return _ok_call_result(n, attempts=2, model_attempts=99)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    result = run_verdicts("текст", run_id, indicators=[1])

    assert result[0].attempts == 2


def test_artifacts_written_for_each_indicator(run_id, tmp_prompts_dir, monkeypatch):
    """8. Артефакты: для каждого индикатора создан 03_verdicts/NN.json."""
    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_default_side_effect))

    run_verdicts("текст", run_id)

    verdicts_dir = run_dir(run_id) / "03_verdicts"
    for n in range(1, 13):
        path = verdicts_dir / f"{n:02d}.json"
        assert path.exists(), f"не найден артефакт {path}"


def test_total_cost_written_to_meta(run_id, tmp_prompts_dir, monkeypatch):
    """9. Суммарная стоимость всех вызовов попала в meta.json."""
    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        return _ok_call_result(n, cost_usd=0.10)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    run_verdicts("текст", run_id)

    meta = load_json(run_id, "meta.json", RunMeta)
    assert meta.cost_usd == pytest.approx(1.20)  # 12 * 0.10


def test_selective_indicators_only_runs_requested(run_id, tmp_prompts_dir, monkeypatch):
    """10. Выборочный запуск: indicators=[3, 7] даёт два вердикта, остальные
    агенты не запускались."""
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(verdicts_module, "call", mock_call)

    result = run_verdicts("текст", run_id, indicators=[3, 7])

    assert len(result) == 2
    assert [v.indicator for v in result] == [3, 7]
    assert mock_call.call_count == 2


def test_real_parallelism_faster_than_sum_of_delays(run_id, tmp_prompts_dir, monkeypatch):
    """11. Параллельность реальная: пять задач с искусственной задержкой 0.3с
    каждая отрабатывают заметно быстрее суммы задержек (1.5с)."""
    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        time_module.sleep(0.3)
        return _ok_call_result(n)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    started = time_module.monotonic()
    run_verdicts("текст", run_id, indicators=[1, 2, 3, 4, 5], max_workers=5)
    duration = time_module.monotonic() - started

    assert duration < 1.0  # заведомо меньше последовательных 5*0.3=1.5с


def test_summary_not_sent_to_agents(run_id, tmp_prompts_dir, monkeypatch):
    """Саммари в stdin агента НЕ попадает — только полный текст акта (решение
    владельца: пруф обязан быть дословной цитатой из акта, цитировать из
    саммари нечего). Документ содержит текст акта и не содержит куска саммари."""
    seen_documents: dict[int, str] = {}

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        seen_documents[n] = document
        return _ok_call_result(n)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    run_verdicts("ПОЛНЫЙ ТЕКСТ МАРКЕР", run_id, indicators=[1, 10])

    for n in (1, 10):
        doc = seen_documents[n]
        assert "ПОЛНЫЙ ТЕКСТ АКТА" in doc
        assert "ПОЛНЫЙ ТЕКСТ МАРКЕР" in doc
        assert "САММАРИ" not in doc


def test_permission_denials_logged_with_indicator_number(run_id, tmp_prompts_dir, monkeypatch):
    """Непустой permission_denials у индикатора -> предупреждение в лог с его номером."""
    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        result = _ok_call_result(n)
        if n == 6:
            result.permission_denials = ["WebSearch"]
        return result

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    run_verdicts("текст", run_id, indicators=[6])

    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "indicator=6" in log_text
    assert "permission_denials" in log_text


def test_on_verdict_called_for_each_indicator_as_it_becomes_ready(
    run_id, tmp_prompts_dir, monkeypatch,
):
    """Колбэк on_verdict вызывается по мере готовности КАЖДОГО индикатора, до
    барьера: к моменту возврата узла собраны все двенадцать, а вызов по
    медленному индикатору приходит позже, чем по быстрым."""
    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        if n == 7:
            time_module.sleep(0.3)  # заведомо медленнее остальных
        return _ok_call_result(n)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    seen: list[int] = []
    run_verdicts("текст", run_id, on_verdict=lambda v: seen.append(v.indicator))

    assert sorted(seen) == list(range(1, 13))
    assert seen[-1] == 7  # медленный индикатор пришёл последним, а не по порядку


def test_on_verdict_receives_error_verdict_too(run_id, tmp_prompts_dir, monkeypatch):
    """Провал индикатора тоже приходит колбэком — вердиктом "ошибка"."""
    monkeypatch.setattr(
        verdicts_module, "call",
        Mock(side_effect=_side_effect_with_overrides({4: _failed_call_result()})),
    )

    seen: dict[int, str] = {}
    run_verdicts("текст", run_id, on_verdict=lambda v: seen.update({v.indicator: v.verdict}))

    assert seen[4] == "ошибка"
    assert seen[1] == "соблюдён"


def test_on_verdict_failure_does_not_break_the_node(run_id, tmp_prompts_dir, monkeypatch):
    """Исключение внутри колбэка не роняет узел: вердикты собраны все двенадцать,
    причина записана в лог."""
    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_default_side_effect))

    def _broken(verdict: Verdict) -> None:
        raise RuntimeError("страница прогресса недоступна")

    result = run_verdicts("текст", run_id, on_verdict=_broken)

    assert len(result) == 12
    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "on_verdict упал" in log_text
