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
from app.trace import load_json, run_dir, save_json, update_meta

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
    """6. Инструменты: встроенные по маршруту (5 — только открыть страницу, 1, 2 и 6 — ничего,
    остальные — поиск и страницы) и (2.7) Гарант по таблице 6.3 плана. Проверка на всех двенадцати."""
    seen_tools: dict[int, list[str]] = {}
    seen_garant: dict[int, list[str] | None] = {}

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        seen_tools[n] = kwargs["allowed_tools"]
        seen_garant[n] = kwargs.get("mcp_tools")
        return _ok_call_result(n)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    run_verdicts("текст", run_id)

    assert set(seen_tools) == set(range(1, 13))
    for n in range(1, 13):
        if n in {1, 2, 5, 6, 7}:   # 1, 2, 5, 7 — без инструментов с 13.10: только текст акта
            assert seen_tools[n] == [] and seen_garant[n] is None
        else:
            assert seen_tools[n] == ["WebSearch", "WebFetch"]
    assert "garant_find_hyperlinks" in seen_garant[3]
    # выгрузка статьи — только у 4 (Р8; у 1, 2, 5 инструментов нет с 13.10)
    assert {n for n in range(1, 13) if seen_garant[n] and "garant_export_block_html" in seen_garant[n]} == {4}
    assert "garant_sutyazhnik" in seen_garant[10]


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


def test_cost_added_to_existing_meta_not_overwritten(run_id, tmp_prompts_dir, monkeypatch):
    """9а. Стоимость вердиктов ПРИБАВЛЯЕТСЯ к уже записанной в meta.json.

    До вердиктов в meta уже лежит стоимость паспорта. Раньше узел писал
    cost_usd = стоимость вердиктов и затирал её: на прогоне МАДИ
    20260908_092522_d7a6ac из итога пропали ровно 0,83 паспорта (проверено
    2026-09-25 по сырым ответам). В диалоге перезапуск одного индикатора
    затёр бы так стоимость всего прогона.
    """
    update_meta(run_id, cost_usd=0.83)

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        return _ok_call_result(n, cost_usd=0.10)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    run_verdicts("текст", run_id)

    meta = load_json(run_id, "meta.json", RunMeta)
    assert meta.cost_usd == pytest.approx(0.83 + 1.20)


def test_selective_indicators_only_runs_requested(run_id, tmp_prompts_dir, monkeypatch):
    """10. Выборочный запуск: indicators=[3, 7] даёт два вердикта, остальные
    агенты не запускались."""
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(verdicts_module, "call", mock_call)

    result = run_verdicts("текст", run_id, indicators=[3, 7])

    assert len(result) == 2
    assert [v.indicator for v in result] == [3, 7]
    assert mock_call.call_count == 2


# Перезапуск из диалогового режима (план итерации 3, шаг 1.2): уточнение задачи
# дописывается блоком в конец промпта, сырые ответы — в свою папку, кандидат
# можно получить без записи чистого JSON (запись только после «Принять»).
# ----------------------------------------------------------------------


def test_extra_instruction_appended_as_block(run_id, tmp_prompts_dir, monkeypatch):
    """Уточнение задачи уходит в модель блоком после промпта; без него промпт побайтно прежний."""
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(verdicts_module, "call", mock_call)

    run_verdicts("текст", run_id, indicators=[3])
    plain = mock_call.call_args_list[0].args[0]
    assert plain == (tmp_prompts_dir / "02_indicator_03.md").read_text(encoding="utf-8")

    run_verdicts("текст", run_id, indicators=[3], extra_instruction="Проверь все приказы Минтранса.")
    extended = mock_call.call_args_list[1].args[0]
    assert extended.startswith(plain)
    assert "<уточнение_задачи>" in extended and "Проверь все приказы Минтранса." in extended
    assert extended.rstrip().endswith("</уточнение_задачи>")


def test_trace_prefix_moves_raw_trace(run_id, tmp_prompts_dir, monkeypatch):
    """Сырые ответы перезапуска уходят под префикс, а не рядом с сырыми прогона."""
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(verdicts_module, "call", mock_call)

    run_verdicts("текст", run_id, indicators=[6], trace_prefix="dialog/v2/")

    assert mock_call.call_args_list[0].kwargs["trace_name"] == "dialog/v2/03_verdicts/06.raw.json"


def test_save_false_returns_candidate_without_writing(run_id, tmp_prompts_dir, monkeypatch):
    """save=False: вердикт возвращается, чистый JSON не пишется, стоимость учитывается."""
    def _effect(instruction, document, model_cls, **kwargs):
        return _ok_call_result(_n_from_trace_name(kwargs["trace_name"]), cost_usd=0.10)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))

    result = run_verdicts("текст", run_id, indicators=[6], save=False)

    assert [v.indicator for v in result] == [6]
    assert not (run_dir(run_id) / "03_verdicts" / "06.json").exists()
    assert load_json(run_id, "meta.json", RunMeta).cost_usd == pytest.approx(0.10)


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


def test_document_carries_full_act_text_under_marker(run_id, tmp_prompts_dir, monkeypatch):
    """Полный текст акта уходит агенту под заголовком-меткой целиком (пруф обязан
    быть дословной цитатой из акта); пересказов в документе нет. Паспорт при
    вызове без него не добавляется — см. test_no_passport_block_without_passport."""
    seen_documents: dict[int, str] = {}

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        seen_documents[n] = kwargs["shared_context"]   # 2.6: акт — общим контекстом (системный промпт)
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


def test_passport_block_follows_act_text(run_id, monkeypatch, valid_passport):
    """v2: паспорт уходит агенту вторым блоком «## ПАСПОРТ АКТА» после текста акта —
    ровно так описаны входные данные в промптах индикаторов."""
    from app.contracts import ActPassport
    seen = {}

    def _effect(instruction, document, schema, **kwargs):
        seen["doc"] = kwargs["shared_context"]   # 2.6: общим контекстом (системный промпт)
        return _ok_call_result(1)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))
    run_verdicts("ПОЛНЫЙ ТЕКСТ МАРКЕР", run_id, indicators=[1], passport=ActPassport(**valid_passport))
    doc = seen["doc"]
    assert doc.startswith("## ПОЛНЫЙ ТЕКСТ АКТА\n\nПОЛНЫЙ ТЕКСТ МАРКЕР")
    assert "## ПАСПОРТ АКТА" in doc and doc.index("## ПОЛНЫЙ ТЕКСТ АКТА") < doc.index("## ПАСПОРТ АКТА")
    assert '"scope": "внешним лицам"' in doc


def test_no_passport_block_without_passport(run_id, monkeypatch):
    seen = {}

    def _effect(instruction, document, schema, **kwargs):
        seen["doc"] = kwargs["shared_context"]   # 2.6: общим контекстом (системный промпт)
        return _ok_call_result(1)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))
    run_verdicts("текст", run_id, indicators=[1])
    assert "## ПАСПОРТ АКТА" not in seen["doc"]



def test_indicator_answer_uses_strict_contract_with_brief(run_id, tmp_prompts_dir, monkeypatch):
    """2.5: агент индикатора обязан вернуть выжимку для эксперта — узел просит строгий VerdictOutput."""
    from app.contracts import VerdictOutput
    mock = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(verdicts_module, "call", mock)
    run_verdicts("текст акта", run_id, indicators=[8])   # 13.10: у 1–7 свои контракты, 8 — общий
    assert mock.call_args.args[2] is VerdictOutput


def test_indicator01_uses_own_contract_and_saves_checked_documents(run_id, tmp_prompts_dir, monkeypatch):
    """13.10: у индикатора 1 свой контракт — таблица проверенных документов перед вердиктом. Вердикт
    сохраняется прежнего вида (его читают гипотезы и Excel), таблица — рядом, в 01.checks.json."""
    import json
    from app.contracts import Indicator01Output
    out = Indicator01Output(
        indicator=1, name=get_indicator(1).name, rationale="разбор", verdict="нарушен", rationale_brief=["пункт"],
        checked_documents=[{"name": "выписка из реестра", "clause": "п. 2", "held_by_state": True,
                            "held_by": "налоговый орган", "requested_by_body": False}],
    )
    mock = Mock(return_value=CallResult(value=out, ok=True, attempts=1, cost_usd=0.01, session_id="s",
                                        error=None, permission_denials=[], duration_s=0.1))
    monkeypatch.setattr(verdicts_module, "call", mock)
    run_verdicts("текст акта", run_id, indicators=[1])
    assert mock.call_args.args[2] is Indicator01Output
    from app.contracts import Indicator02Output
    from app.nodes.verdicts import INDICATOR_OUTPUT
    assert INDICATOR_OUTPUT[2] is Indicator02Output   # 13.10: у индикатора 2 тоже свой контракт
    from app.contracts import Indicator03Output
    assert INDICATOR_OUTPUT[3] is Indicator03Output   # 13.10: и у индикатора 3
    from app.contracts import Indicator04Output
    assert INDICATOR_OUTPUT[4] is Indicator04Output   # и у индикатора 4
    from app.contracts import Indicator05Output
    assert INDICATOR_OUTPUT[5] is Indicator05Output   # и у индикатора 5
    from app.contracts import Indicator06Output
    assert INDICATOR_OUTPUT[6] is Indicator06Output   # и у индикатора 6
    from app.contracts import Indicator07Output
    assert INDICATOR_OUTPUT[7] is Indicator07Output   # и у индикатора 7
    assert load_json(run_id, "03_verdicts/01.json", Verdict).verdict == "нарушен"
    checks = json.loads((run_dir(run_id) / "03_verdicts/01.checks.json").read_text(encoding="utf-8"))
    assert checks["checked_documents"][0]["held_by_state"] is True



# ---- 2.6: кэш — акт и паспорт общим контекстом, лидеры групп стартуют первыми ----


def test_act_and_passport_go_to_shared_context_not_stdin(run_id, tmp_prompts_dir, monkeypatch):
    seen = []

    def _effect(instruction, document, model_cls, **kwargs):
        seen.append((document, kwargs.get("shared_context")))
        return _default_side_effect(instruction, document, model_cls, **kwargs)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))
    run_verdicts("текст акта", run_id)
    assert len(seen) == 12 and all(document is None for document, _ in seen)
    assert {ctx for _, ctx in seen} == {"## ПОЛНЫЙ ТЕКСТ АКТА\n\nтекст акта"}


def test_group_leaders_start_before_followers(run_id, tmp_prompts_dir, monkeypatch):
    """Лидер каждой группы маршрута (первый текстовый — 1, первый с интернетом — 3) стартует сразу,
    остальные — через CACHE_WARMUP_S: к их старту лидер уже записал акт в кэш."""
    import time
    monkeypatch.setattr(verdicts_module, "CACHE_WARMUP_S", 0.3)
    started = {}

    def _effect(instruction, document, model_cls, **kwargs):
        started[_n_from_trace_name(kwargs["trace_name"])] = time.monotonic()
        return _default_side_effect(instruction, document, model_cls, **kwargs)

    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_effect))
    result = run_verdicts("текст акта", run_id)
    # группы кэша: 1–7 — свои контракты (13.10);
    # остальные — интернет и Гарант, лидер 8
    leaders = {1, 2, 3, 4, 5, 6, 7, 8}
    assert len(result) == 12
    assert max(started[n] for n in leaders) + 0.25 <= min(t for n, t in started.items() if n not in leaders)


def test_single_indicator_recheck_has_no_delay(run_id, tmp_prompts_dir, monkeypatch):
    """Перепроверка одного индикатора из диалога — он сам себе лидер, ждать нечего."""
    import time
    monkeypatch.setattr(verdicts_module, "CACHE_WARMUP_S", 5)
    monkeypatch.setattr(verdicts_module, "call", Mock(side_effect=_default_side_effect))
    t = time.monotonic()
    run_verdicts("текст акта", run_id, indicators=[7])
    assert time.monotonic() - t < 2


# ---- 2.7 (итерация 4, К): регионы сравнения — подстановкой в промпт ----


def test_region_labels_filled_from_config(run_id, tmp_prompts_dir, monkeypatch):
    """Метки регионов в промпте заменяются списком из конфига; число — цифрами."""
    from app.config import COMPARISON_REGIONS
    (tmp_prompts_dir / "02_indicator_07.md").write_text(
        "Порталы {{ЧИСЛО_РЕГИОНОВ}} субъектов сравнения — {{РЕГИОНЫ_СРАВНЕНИЯ}}.", encoding="utf-8")
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(verdicts_module, "call", mock_call)
    run_verdicts("текст", run_id, indicators=[7])
    instruction = mock_call.call_args_list[0].args[0]
    assert "{{" not in instruction
    assert instruction == f"Порталы {len(COMPARISON_REGIONS)} субъектов сравнения — {', '.join(COMPARISON_REGIONS)}."


def test_region_labels_filled_from_run_list(run_id, tmp_prompts_dir, monkeypatch):
    """Список регионов прогона (настройки прогона) важнее списка по умолчанию."""
    (tmp_prompts_dir / "02_indicator_11.md").write_text(
        "Начинай с {{ЧИСЛО_РЕГИОНОВ}} субъектов сравнения: {{РЕГИОНЫ_СРАВНЕНИЯ}}.", encoding="utf-8")
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(verdicts_module, "call", mock_call)
    run_verdicts("текст", run_id, indicators=[11], regions=["Санкт-Петербург", "Казань"])
    assert mock_call.call_args_list[0].args[0] == "Начинай с 2 субъектов сравнения: Санкт-Петербург, Казань."



def test_search_limit_label_filled_per_indicator(run_id, tmp_prompts_dir, monkeypatch):
    """Л: потолок заходов — из настроек прогона, иначе из SEARCH_LIMITS конфига."""
    from app.config import SEARCH_LIMITS
    (tmp_prompts_dir / "02_indicator_03.md").write_text("ПОТОЛОК — {{ПОТОЛОК_ЗАХОДОВ}} ЗАХОДОВ.", encoding="utf-8")
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(verdicts_module, "call", mock_call)
    run_verdicts("текст", run_id, indicators=[3])
    assert mock_call.call_args_list[0].args[0] == f"ПОТОЛОК — {SEARCH_LIMITS[3]} ЗАХОДОВ."
    run_verdicts("текст", run_id, indicators=[3], search_limits={3: 40})
    assert mock_call.call_args_list[1].args[0] == "ПОТОЛОК — 40 ЗАХОДОВ."



def test_indicator_3_gets_longer_timeout(run_id, tmp_prompts_dir, monkeypatch):
    """Индикатор 3 проверяет статус каждой ссылки в правовой базе — ему таймаут больше общего (живая проверка 12.10)."""
    from app.config import INDICATOR_TIMEOUT_S
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(verdicts_module, "call", mock_call)
    run_verdicts("текст", run_id, indicators=[3, 4], timeout_s=900)
    by_n = {_n_from_trace_name(c.kwargs["trace_name"]): c.kwargs["timeout_s"] for c in mock_call.call_args_list}
    assert by_n[3] == INDICATOR_TIMEOUT_S[3] > 900 and by_n[4] == 900
