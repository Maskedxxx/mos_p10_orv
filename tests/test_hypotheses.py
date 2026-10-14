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
        grounds="основание из карточки",
        effects=[Effect(affected="бизнес", metric="число обращений", how_to_calculate="сумма за год", expected_trend="рост показателя относительно года до вступления акта подтвердит гипотезу")],
        data=[DataItem(metric="число заявок", source="открытый", where="Мосстат")],
    )
    value = HypothesisSet(
        indicator=n, name=f"индикатор {n}", violation_kind="издержки", why_this_many="выявлено одно нарушение",
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


def test_act_in_shared_context_and_verdict_card_in_stdin(run_id, tmp_prompts, monkeypatch):
    """6. 2.6: текст акта — общим контекстом (системный промпт, кэш для всех агентов-гипотез),
    карточка вердикта — в stdin; они различимы и не смешиваются."""
    seen: dict[int, tuple[str, str]] = {}

    def _effect(instruction, document, model_cls, **kwargs):
        n = _n_from_trace_name(kwargs["trace_name"])
        seen[n] = (document, kwargs["shared_context"])
        return _ok_call_result(n)

    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_effect))

    run_hypotheses_violated(_buckets_violated([1]), "ПОЛНЫЙ ТЕКСТ МАРКЕР", run_id)

    card, shared = seen[1]
    assert shared.startswith("## ПОЛНЫЙ ТЕКСТ АКТА") and "ПОЛНЫЙ ТЕКСТ МАРКЕР" in shared
    assert card.startswith("## КАРТОЧКА ВЕРДИКТА (JSON)") and '"verdict": "нарушен"' in card
    assert "ПОЛНЫЙ ТЕКСТ МАРКЕР" not in card and "КАРТОЧКА ВЕРДИКТА" not in shared


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

    run_hypotheses_no_data(_buckets_no_data([10]), "текст", run_id)   # 10, не 3: индикаторы 1-6 узел пропускает (Б1)

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
            run_hypotheses_no_data(_buckets_no_data([10, 8]), "текст", rid)   # оба из семейства «практика» (Б1)
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


def test_hypotheses_extra_instruction_prefix_and_no_save(run_id, tmp_prompts, monkeypatch):
    """Диалог (шаг 1.2): уточнение блоком в конце промпта, трасса под префиксом, без записи при save=False."""
    violated_path, _ = tmp_prompts
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(hyp_module, "call", mock_call)

    sets = run_hypotheses_violated(
        _buckets_violated([7]), "текст", run_id,
        extra_instruction="Учти пятнадцать форм отчётности.", trace_prefix="dialog/v3/", save=False,
    )

    instruction = mock_call.call_args_list[0].args[0]
    assert instruction.startswith(violated_path.read_text(encoding="utf-8"))
    assert "<уточнение_задачи>" in instruction and "Учти пятнадцать форм отчётности." in instruction
    assert mock_call.call_args_list[0].kwargs["trace_name"] == "dialog/v3/05_hypotheses/07.raw.json"
    assert [s.indicator for s in sets] == [7]
    assert not (run_dir(run_id) / "05_hypotheses" / "07.json").exists()


def test_hypotheses_defaults_unchanged(run_id, tmp_prompts, monkeypatch):
    """Без новых параметров промпт побайтно прежний, трасса и запись — как раньше."""
    violated_path, _ = tmp_prompts
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(hyp_module, "call", mock_call)

    run_hypotheses_violated(_buckets_violated([7]), "текст", run_id)

    assert mock_call.call_args_list[0].args[0] == violated_path.read_text(encoding="utf-8")
    assert mock_call.call_args_list[0].kwargs["trace_name"] == "05_hypotheses/07.raw.json"
    assert (run_dir(run_id) / "05_hypotheses" / "07.json").exists()


def test_no_data_skips_text_family_indicators(run_id, tmp_prompts, monkeypatch):
    """Б1 (ОС 2026-09-04). «Нет данных» у индикаторов 1-6 — незавершённая проверка
    по тексту или правовой базе, а не пробел в практике: узел [4б] для них
    не вызывается, гипотез нет. Индикаторы 7-12 обрабатываются как прежде."""
    mock_call = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(hyp_module, "call", mock_call)

    result = run_hypotheses_no_data(_buckets_no_data([3, 4, 10, 11]), "текст", run_id)

    assert mock_call.call_count == 2
    assert [s.indicator for s in result] == [10, 11]


def test_passport_block_between_text_and_card(run_id, tmp_prompts, monkeypatch, valid_passport):
    """v2: текст акта → паспорт → карточка вердикта, как во входных данных 04a/04b."""
    from app.contracts import ActPassport
    seen = {}

    def _effect(instruction, document, schema, **kwargs):
        seen["doc"] = kwargs["shared_context"] + document   # 2.6: в системном промпте, затем stdin
        return _ok_call_result(1)

    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_effect))
    run_hypotheses_violated(_buckets_violated([1]), "ТЕКСТ", run_id, passport=ActPassport(**valid_passport))
    doc = seen["doc"]
    a = doc.index("## ПОЛНЫЙ ТЕКСТ АКТА"); b = doc.index("## ПАСПОРТ АКТА"); c = doc.index("## КАРТОЧКА ВЕРДИКТА (JSON)")
    assert a < b < c



def test_hypotheses_answer_uses_strict_contract_with_brief(run_id, tmp_prompts, monkeypatch):
    """2.5: агенты 4a / 4b обязаны вернуть выжимки — узел просит строгий HypothesisSetOutput."""
    from app.contracts import HypothesisSetOutput
    mock = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(hyp_module, "call", mock)
    run_hypotheses_violated(_buckets_violated([7]), "текст акта", run_id)
    assert mock.call_args.args[2] is HypothesisSetOutput



def test_first_hypothesis_call_starts_before_others(run_id, tmp_prompts, monkeypatch):
    """2.6: первый агент ветки стартует сразу и пишет акт в кэш, остальные — через CACHE_WARMUP_S."""
    import time
    monkeypatch.setattr(hyp_module, "CACHE_WARMUP_S", 0.3)
    started: dict[int, float] = {}

    def _effect(instruction, document, model_cls, **kwargs):
        started[_n_from_trace_name(kwargs["trace_name"])] = time.monotonic()
        return _ok_call_result(_n_from_trace_name(kwargs["trace_name"]))

    monkeypatch.setattr(hyp_module, "call", Mock(side_effect=_effect))
    run_hypotheses_violated(_buckets_violated([7, 8, 9]), "текст", run_id)
    assert started[7] + 0.25 <= min(started[8], started[9])


# ---- 14.10: числа для органа из вердикта доходят до гипотез целиком ----


def test_to_request_goes_to_card_and_missing_numbers_added_by_code(run_id, tmp_prompts, monkeypatch):
    """Список to_request индикатора (03_verdicts/10.checks.json) — в карточку агенту отдельным блоком; число,
    которое агент не положил в data, код дописывает в первую гипотезу; уже положенное не дублируется."""
    import json
    to_request = [{"metric": "число заявок", "unit": "ед.", "clause": "п. 2"},
                  {"metric": "число несостоявшихся отборов", "unit": "ед.", "clause": "п. 2.19"}]
    path = run_dir(run_id) / "03_verdicts/10.checks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"findings": [], "to_request": to_request}, ensure_ascii=False), encoding="utf-8")
    mock = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(hyp_module, "call", mock)
    sets = run_hypotheses_no_data(_buckets_no_data([10]), "текст акта", run_id)
    assert "## ЧИСЛА ДЛЯ ОРГАНА (JSON)" in mock.call_args.args[1] and "несостоявшихся" in mock.call_args.args[1]
    metrics = [d.metric for d in sets[0].hypotheses[0].data]
    assert metrics == ["число заявок", "число несостоявшихся отборов, ед. (п. 2.19)"]   # «число заявок» уже было
    assert sets[0].hypotheses[0].data[-1].source == "запрос органу"
    saved = load_json(run_id, "05_hypotheses/10.json", HypothesisSet)
    assert [d.metric for d in saved.hypotheses[0].data] == metrics


def test_no_to_request_card_unchanged(run_id, tmp_prompts, monkeypatch):
    """Без таблицы индикатора карточка прежняя — блока чисел нет, данные агента не трогаются."""
    mock = Mock(side_effect=_default_side_effect)
    monkeypatch.setattr(hyp_module, "call", mock)
    sets = run_hypotheses_violated(_buckets_violated([4]), "текст акта", run_id)
    assert "ЧИСЛА ДЛЯ ОРГАНА" not in mock.call_args.args[1]
    assert [d.metric for d in sets[0].hypotheses[0].data] == ["число заявок"]


def test_ensure_requests_recognizes_reworded_metric():
    """Проба 152-ПП 14.10: агент вставил пункт в середину названия — это то же число, дубль не дописывается."""
    from app.nodes.hypotheses import _ensure_requests
    hset = _ok_call_result(10).value
    first = hset.hypotheses[0].model_copy(update={"data": [DataItem(
        metric="число поданных заявок на субсидию (приложение 1, п. 2.1), ед., по годам 2023-2025",
        source="запрос органу")]})
    hset = hset.model_copy(update={"hypotheses": [first]})
    out, added = _ensure_requests(hset, [{"metric": "число поданных заявок на субсидию (приложение 1)",
                                          "unit": "ед.", "clause": "п. 2.1"}])
    assert added == [] and len(out.hypotheses[0].data) == 1


def test_questions_moved_to_data_as_before():
    """14.10: вопросы из questions код кладёт в data с источником «вопрос органу» — дальше по цепочке как прежде."""
    from app.contracts import HypothesisSetOutput
    from app.nodes.hypotheses import _questions_to_data
    hyp = {"hypothesis": "г", "grounds": "о", "proof": None, "clause": None,
           "effects": [{"affected": "бизнес", "metric": "м", "how_to_calculate": "к",
                        "expected_trend": "рост показателя относительно года до вступления акта подтвердит гипотезу",
                        "how_to_calculate_brief": ["к"], "expected_trend_brief": ["рост"]}],
           "data": [{"metric": "число заявок", "source": "запрос органу"}],
           "questions": [{"clause": "п. 2.7", "why": "причины", "question": "Какие основания продления чаще всего?"}],
           "hypothesis_brief": ["г"], "grounds_brief": ["о"]}
    value = HypothesisSetOutput(indicator=11, name="н", violation_kind="издержки", why_this_many="одна", hypotheses=[hyp])
    hset = _questions_to_data(value)
    assert [(d.metric, d.source) for d in hset.hypotheses[0].data] == [
        ("число заявок", "запрос органу"), ("Какие основания продления чаще всего? (п. 2.7)", "вопрос органу")]
