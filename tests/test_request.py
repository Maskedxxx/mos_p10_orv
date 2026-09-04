"""
Тесты app/nodes/request.py (узел [6], сборка запроса показателей одним вызовом модели).

runner.call подменяется моком — реально прогоняется только логика узла
(сборка stdin из карты, обработка провала, две проверки-предупреждения,
трейсы, прибавление стоимости). Промпт 06_request.md подменяется фикстурным
файлом во временном каталоге.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import app.nodes.request as request_module
from app.contracts import DataItem, DataRequest, Effect, HypothesisMap, MapRow, RequestItem, RunMeta, SourceRef
from app.nodes.request import _request_years, build_request
from app.runner import CallResult
from app.trace import load_json, run_dir, save_json


def _row(row_id: int, *, source: str = "запрос органу") -> MapRow:
    return MapRow(
        row_id=row_id,
        indicators=[1],
        hypothesis=f"гипотеза {row_id}",
        effect=Effect(metric="число обращений", how_to_calculate="сумма за год"),
        data=[DataItem(metric=f"показатель {row_id}", source=source)],
    )


def _map(rows: list[MapRow]) -> HypothesisMap:
    return HypothesisMap(rows=rows, merges=[])


@pytest.fixture
def tmp_prompt(tmp_path, monkeypatch):
    """Подменить PROMPT_REQUEST на фиктивный файл во временном каталоге."""
    path = tmp_path / "06_request.md"
    path.write_text("Промпт 06_request (фикстура).", encoding="utf-8")
    monkeypatch.setattr(request_module, "PROMPT_REQUEST", path)
    return path


def _make_run(rid: str, *, cost_usd: float = 0.0) -> None:
    meta = RunMeta(
        run_id=rid, act_filename="act.docx",
        started_at="2026-01-01T12:00:00", status="started", cost_usd=cost_usd,
    )
    save_json(rid, "meta.json", meta)


@pytest.fixture
def run_id(tmp_runs_dir) -> str:
    rid = "run_request_test"
    _make_run(rid)
    return rid


def _ok_call_result(value: DataRequest, *, cost_usd: float = 0.04) -> CallResult[DataRequest]:
    return CallResult(
        value=value, ok=True, attempts=1, cost_usd=cost_usd, session_id="s",
        error=None, permission_denials=[], duration_s=1.0,
    )


def _failed_call_result(*, cost_usd: float = 0.01) -> CallResult[DataRequest]:
    return CallResult(
        value=None, ok=False, attempts=3, cost_usd=cost_usd, session_id=None,
        error="таймаут процесса после 900 с", permission_denials=[], duration_s=1.0,
    )


def test_only_internal_source_reaches_result(run_id, tmp_prompt, monkeypatch):
    """1. В результат попадают только показатели, которые модель честно взяла из
    строк с источником «запрос органу» (модель отбирает сама; узел это не проверяет
    построчно, но не должен предупреждать зря на корректном ответе)."""
    hyp_map = _map([_row(1, source="запрос органу"), _row(2, source="открытый")])
    req = DataRequest(
        items=[RequestItem(num="1", metric="показатель 1", for_rows=[1])],
        years=[2024, 2025, 2026],
    )
    monkeypatch.setattr(request_module, "call", Mock(return_value=_ok_call_result(req)))

    result = build_request(hyp_map, run_id)

    assert [i.metric for i in result.items] == ["показатель 1"]
    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "притащила лишнее" not in log_text


def test_nonexistent_for_rows_is_caught(run_id, tmp_prompt, monkeypatch):
    """2. Несуществующий номер в for_rows -> ошибка в лог с указанием номера."""
    hyp_map = _map([_row(1, source="запрос органу")])
    req = DataRequest(
        items=[RequestItem(num="1", metric="показатель 1", for_rows=[1, 99])],
        years=[2024, 2025, 2026],
    )
    monkeypatch.setattr(request_module, "call", Mock(return_value=_ok_call_result(req)))

    build_request(hyp_map, run_id)

    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "ERROR" in log_text
    assert "несуществующую строку карты 99" in log_text
    assert "№ 1" in log_text


def test_open_source_only_row_is_warned(run_id, tmp_prompt, monkeypatch):
    """Показатель ссылается только на строку без «запрос органу» -> предупреждение в лог."""
    hyp_map = _map([_row(1, source="открытый")])
    req = DataRequest(
        items=[RequestItem(num="1", metric="показатель 1", for_rows=[1])],
        years=[2024, 2025, 2026],
    )
    monkeypatch.setattr(request_module, "call", Mock(return_value=_ok_call_result(req)))

    build_request(hyp_map, run_id)

    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "WARNING" in log_text
    assert "притащила лишнее" in log_text


def test_model_failure_gives_empty_request(run_id, tmp_prompt, monkeypatch):
    """3. Провал модели -> DataRequest с пустым items и годами по умолчанию."""
    hyp_map = _map([_row(1)])
    monkeypatch.setattr(request_module, "call", Mock(return_value=_failed_call_result()))

    result = build_request(hyp_map, run_id)

    assert result.items == []
    assert len(result.years) == 3
    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "провалился" in log_text


def test_artifacts_written(run_id, tmp_prompt, monkeypatch):
    """4. Артефакт 07_request.json создан."""
    hyp_map = _map([_row(1)])
    req = DataRequest(
        items=[RequestItem(num="1", metric="показатель 1", for_rows=[1])],
        years=[2024, 2025, 2026],
    )
    monkeypatch.setattr(request_module, "call", Mock(return_value=_ok_call_result(req)))

    build_request(hyp_map, run_id)

    saved = load_json(run_id, "07_request.json", DataRequest)
    assert saved.items[0].metric == "показатель 1"


def test_cost_added_not_overwritten(tmp_runs_dir, tmp_prompt, monkeypatch):
    """5. Стоимость ПРИБАВЛЯЕТСЯ к уже записанной в meta.json, а не затирает её."""
    rid = "run_request_cost_test"
    _make_run(rid, cost_usd=3.0)

    hyp_map = _map([_row(1)])
    req = DataRequest(
        items=[RequestItem(num="1", metric="показатель 1", for_rows=[1])],
        years=[2024, 2025, 2026],
    )
    monkeypatch.setattr(
        request_module, "call", Mock(return_value=_ok_call_result(req, cost_usd=0.2)),
    )

    build_request(hyp_map, rid)

    meta = load_json(rid, "meta.json", RunMeta)
    assert meta.cost_usd == pytest.approx(3.0 + 0.2)


def test_no_tools_and_prompt_and_trace_name(run_id, tmp_prompt, monkeypatch):
    """Инструменты пустые, используется промпт 06_request.md, верное имя трейса."""
    hyp_map = _map([_row(1)])
    req = DataRequest(items=[], years=[2024, 2025, 2026])
    seen = {}

    def _effect(instruction, document, model_cls, **kwargs):
        seen["instruction"] = instruction
        seen["tools"] = kwargs["allowed_tools"]
        seen["trace_name"] = kwargs["trace_name"]
        seen["document"] = document
        return _ok_call_result(req)

    monkeypatch.setattr(request_module, "call", Mock(side_effect=_effect))

    build_request(hyp_map, run_id)

    assert seen["tools"] == []
    assert "Промпт 06_request" in seen["instruction"]
    assert seen["trace_name"] == "07_request.json".replace(".json", ".raw.json")
    assert "## КАРТА ГИПОТЕЗ (JSON)" in seen["document"]
    assert '"row_id": 1' in seen["document"]


def test_empty_map_skips_model_call(run_id, tmp_prompt, monkeypatch):
    """Пустая карта (нет строк) -> модель не вызывается, пустой запрос сохраняется."""
    mock_call = Mock()
    monkeypatch.setattr(request_module, "call", mock_call)

    result = build_request(_map([]), run_id)

    assert result.items == []
    assert len(result.years) == 3
    mock_call.assert_not_called()


# ---- ОС экспертов 2026-09-04, узел 6, находки Г1/Г4/Г5/Г6 ----


def _row2(row_id: int, metrics: list[tuple[str, str]]) -> MapRow:
    """Строка карты с несколькими показателями: [(metric, source), ...]."""
    return MapRow(
        row_id=row_id, indicators=[1], hypothesis=f"гипотеза {row_id}",
        effect=Effect(metric="число обращений", how_to_calculate="сумма за год"),
        data=[DataItem(metric=m, source=src, where=("Росстат" if src == "открытый" else None)) for m, src in metrics],
    )


def _item(num: str, metric: str, rows: list[int], sources: list[tuple[int, str]], **kw) -> RequestItem:
    return RequestItem(
        num=num, metric=metric, for_rows=rows,
        sources=[SourceRef(row_id=r, metric=m) for r, m in sources], **kw,
    )


def test_coverage_missing_internal_metric_is_logged_as_error(run_id, tmp_prompt, monkeypatch):
    """Г4. Показатель «запрос органу» из карты не вошёл ни в одну позицию и не назван
    в why_dropped — потеря. ERROR в лог, как потеря строки в узле 5."""
    hyp_map = _map([_row2(1, [("число заявок", "запрос органу"), ("число отказов", "запрос органу")])])
    req = DataRequest(items=[_item("1", "число заявок (п. 1)", [1], [(1, "число заявок")], unit="ед.")], years=[2025])
    monkeypatch.setattr(request_module, "call", Mock(return_value=_ok_call_result(req)))
    build_request(hyp_map, run_id)
    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "ERROR" in log_text and "не вошёл ни в одну позицию" in log_text and "число отказов" in log_text


def test_coverage_dropped_with_explanation_is_not_error(run_id, tmp_prompt, monkeypatch):
    """Г4, граница: показатель не взят, но назван в why_dropped — не потеря, INFO."""
    hyp_map = _map([_row2(1, [("число заявок", "запрос органу"), ("позиция органа по вопросу", "запрос органу")])])
    req = DataRequest(
        items=[_item("1", "число заявок (п. 1)", [1], [(1, "число заявок")], unit="ед.")], years=[2025],
        why_dropped="строка 1, «позиция органа по вопросу» — не показатель и не перечень",
    )
    monkeypatch.setattr(request_module, "call", Mock(return_value=_ok_call_result(req)))
    build_request(hyp_map, run_id)
    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "не вошёл ни в одну позицию" not in log_text


def test_coverage_duplicate_source_is_logged_as_warning(run_id, tmp_prompt, monkeypatch):
    """Г4. Один показатель карты в двух позициях — либо дубль (ГРАНТЫ № 6 и № 7:
    «а в чём отличие от предыдущего?»), либо законное разделение «сумма и количество»
    на две позиции (Г1). Различить может только аналитик — WARNING, не ERROR."""
    hyp_map = _map([_row2(1, [("число отборов", "запрос органу")])])
    req = DataRequest(items=[
        _item("1", "число отборов проведённых (п. 2.13)", [1], [(1, "число отборов")], unit="ед."),
        _item("2", "число отборов объявленных (п. 1.7)", [1], [(1, "число отборов")], unit="ед."),
    ], years=[2025])
    monkeypatch.setattr(request_module, "call", Mock(return_value=_ok_call_result(req)))
    build_request(hyp_map, run_id)
    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "WARNING" in log_text and "в несколько позиций" in log_text
    assert "ERROR" not in log_text


def test_coverage_skipped_when_model_gave_no_sources(run_id, tmp_prompt, monkeypatch):
    """Г4, совместимость: ответ по промпту v1 без sources — покрытие не проверяется,
    в лог WARNING, ERROR нет."""
    hyp_map = _map([_row2(1, [("число заявок", "запрос органу")])])
    req = DataRequest(items=[RequestItem(num="1", metric="число заявок, ед.", for_rows=[1])], years=[2025])
    monkeypatch.setattr(request_module, "call", Mock(return_value=_ok_call_result(req)))
    build_request(hyp_map, run_id)
    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "покрытие не проверить" in log_text and "не вошёл ни в одну позицию" not in log_text


def test_two_measures_in_one_item_logged_as_error(run_id, tmp_prompt, monkeypatch):
    """Г1. «количество и доля» в одной позиции — ERROR (12 из 154 позиций в ОС)."""
    hyp_map = _map([_row2(1, [("число отказов", "запрос органу")])])
    req = DataRequest(items=[_item("1", "количество и доля уведомлений с отказом (п. 10)", [1], [(1, "число отказов")], unit="ед.")], years=[2025])
    monkeypatch.setattr(request_module, "call", Mock(return_value=_ok_call_result(req)))
    build_request(hyp_map, run_id)
    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "ERROR" in log_text and "две величины" in log_text


def test_request_years_clipped_to_effective_year():
    """Г5. Акт вступил в силу 21.08.2025 — колонок 2023 и 2024 в бланке нет
    (1112: 6 подстрок «по подпериодам» — модель выражала это разрезом)."""
    assert _request_years(effective_year=2025, end_year=2025) == [2025]
    assert _request_years(effective_year=2020, end_year=2025) == [2023, 2024, 2025]
    assert _request_years(effective_year=None, end_year=2025) == [2023, 2024, 2025]


def test_request_years_future_act_gives_effective_year_only():
    """Г5, граница: акт вступает позже конца окна — единственная колонка года
    вступления (на решение владельца, см. разбор)."""
    assert _request_years(effective_year=2027, end_year=2025) == [2027]


def test_document_carries_act_passport_and_years(run_id, tmp_prompt, monkeypatch):
    """Г6. В stdin узла 6 уходят текст акта, паспорт и годы бланка — раньше только карта."""
    seen = {}

    def _effect(instruction, document, schema, **kwargs):
        seen["doc"] = document
        return _ok_call_result(DataRequest(items=[], years=[2025]))

    monkeypatch.setattr(request_module, "call", Mock(side_effect=_effect))
    hyp_map = _map([_row2(1, [("число заявок", "запрос органу")])])
    build_request(hyp_map, run_id, document_md="ТЕКСТ АКТА ЗДЕСЬ",
                  passport={"requisites": {"authority": "Департамент"}, "status": {"status": "принят", "effective_date": "21.08.2025"}},
                  effective_year=2025)
    doc = seen["doc"]
    assert "## ПОЛНЫЙ ТЕКСТ АКТА" in doc and "ТЕКСТ АКТА ЗДЕСЬ" in doc
    assert "## ПАСПОРТ АКТА" in doc and "Департамент" in doc
    assert "## КАРТА ГИПОТЕЗ (JSON)" in doc
    assert "## ГОДЫ БЛАНКА (JSON)" in doc
    assert doc.index("## ПОЛНЫЙ ТЕКСТ АКТА") < doc.index("## ПАСПОРТ АКТА") < doc.index("## КАРТА ГИПОТЕЗ (JSON)") < doc.index("## ГОДЫ БЛАНКА (JSON)")


def test_result_years_taken_from_code_not_model(run_id, tmp_prompt, monkeypatch):
    """Г5. Годы в результате — те, что посчитал код, даже если модель вернула свои."""
    hyp_map = _map([_row2(1, [("число заявок", "запрос органу")])])
    req = DataRequest(items=[], years=[2019, 2020, 2021])
    monkeypatch.setattr(request_module, "call", Mock(return_value=_ok_call_result(req)))
    value = build_request(hyp_map, run_id, effective_year=2025)
    assert value.years == _request_years(effective_year=2025)
