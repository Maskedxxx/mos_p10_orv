"""
Тесты app/nodes/passport.py (узел [1.1], паспорт акта вместо саммари).

runner.call подменяется моком — прогоняется логика узла: сборка stdin, схема,
таймаут, сохранение артефакта, стоимость, поведение при провале; плюс вывод
года вступления в силу и блок паспорта для агентов.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

import app.nodes.passport as passport_module
from app.config import PASSPORT_TIMEOUT_S
from app.contracts import ActPassport, RunMeta
from app.nodes.passport import make_passport, passport_block, passport_effective_year
from app.runner import CallResult
from app.trace import load_json, run_dir, save_json


@pytest.fixture
def run_id(tmp_runs_dir) -> str:
    rid = "run_passport_test"
    save_json(rid, "meta.json", RunMeta(run_id=rid, act_filename="act.docx",
                                        started_at="2026-01-01T12:00:00", status="started", cost_usd=1.0))
    return rid


def _ok(passport: ActPassport, cost: float = 0.9) -> CallResult[ActPassport]:
    return CallResult(value=passport, ok=True, attempts=1, cost_usd=cost, session_id="s",
                      error=None, permission_denials=[], duration_s=500.0)


def _failed(cost: float = 1.3) -> CallResult[ActPassport]:
    return CallResult(value=None, ok=False, attempts=3, cost_usd=cost, session_id=None,
                      error="API Error: Response stalled mid-stream", permission_denials=[], duration_s=640.0)


def test_make_passport_saves_artifact_and_returns_model(run_id, monkeypatch, valid_passport):
    passport = ActPassport(**valid_passport)
    mock_call = Mock(return_value=_ok(passport))
    monkeypatch.setattr(passport_module, "call", mock_call)

    result = make_passport("ТЕКСТ АКТА", run_id)

    assert result == passport
    saved = load_json(run_id, "02_passport.json", ActPassport)
    assert saved.requisites.authority == "Департамент"
    # stdin — только текст акта под меткой; схема — ответ модели ActPassportOutput (15.10); без инструментов; свой таймаут
    args, kwargs = mock_call.call_args
    assert args[1] == "## ПОЛНЫЙ ТЕКСТ АКТА\n\nТЕКСТ АКТА"
    from app.contracts import ActPassportOutput
    assert args[2] is ActPassportOutput
    assert kwargs["allowed_tools"] == []
    assert kwargs["timeout_s"] == PASSPORT_TIMEOUT_S
    assert kwargs["trace_name"] == "02_passport.raw.json"


def test_make_passport_adds_cost_to_meta(run_id, monkeypatch, valid_passport):
    monkeypatch.setattr(passport_module, "call", Mock(return_value=_ok(ActPassport(**valid_passport), cost=0.9)))
    make_passport("текст", run_id)
    assert load_json(run_id, "meta.json", RunMeta).cost_usd == pytest.approx(1.0 + 0.9)


def test_make_passport_failure_returns_none_and_logs(run_id, monkeypatch):
    """Провал вызова — None и WARNING; остановить прогон решает оркестратор.
    Стоимость упавших попыток всё равно учитывается."""
    monkeypatch.setattr(passport_module, "call", Mock(return_value=_failed(cost=1.3)))
    assert make_passport("текст", run_id) is None
    log_text = (run_dir(run_id) / "run.log").read_text(encoding="utf-8")
    assert "WARNING" in log_text and "stalled" in log_text
    assert not (run_dir(run_id) / "02_passport.json").exists()
    assert load_json(run_id, "meta.json", RunMeta).cost_usd == pytest.approx(2.3)


def test_passport_block_has_heading_and_json(valid_passport):
    block = passport_block(ActPassport(**valid_passport))
    assert block.startswith("## ПАСПОРТ АКТА\n\n```json\n")
    assert '"status": "принят"' in block
    assert passport_block(None) == ""


@pytest.mark.parametrize("date, status, expected", [
    ("21.08.2025", "принят", 2025),
    ("1 января 2027 г.", "принят", 2027),
    (None, "принят", None),
    ("в тексте не указана", "принят", None),
    ("21.08.2025", "проект", None),
])
def test_passport_effective_year(valid_passport, date, status, expected):
    data = {**valid_passport, "status": {**valid_passport["status"], "status": status, "effective_date": date}}
    if status == "проект":
        data["requisites"] = {**data["requisites"], "number": None, "date": None}
    assert passport_effective_year(ActPassport(**data)) == expected


def test_passport_effective_year_none_without_passport():
    assert passport_effective_year(None) is None



# ---- 2.7 (итерация 4, А): орган-разработчик ----


def test_passport_agent_gets_garant_search(run_id, monkeypatch, valid_passport, tmp_path):
    """Разработчика агент паспорта ищет и в Гаранте: кто издаёт приказы во исполнение акта."""
    import app.runner as runner_mod
    token = tmp_path / "token"
    token.write_text("T-1\n", encoding="utf-8")
    monkeypatch.setattr(runner_mod, "GARANT_TOKEN_FILE", token)
    mock_call = Mock(return_value=_ok(ActPassport(**valid_passport)))
    monkeypatch.setattr(passport_module, "call", mock_call)
    passport_module.make_passport("текст", run_id)
    assert mock_call.call_args.kwargs["mcp_tools"] == ["garant_search"]


def test_developers_field_and_user_choice(valid_passport):
    """Модель пишет разработчиков списком; выбор пользователя при загрузке важнее ответа модели."""
    from app.contracts import Requisites
    desc = Requisites.model_fields["developers"].description
    assert "Не нашёл — пустой список, не угадывай" in desc
    p = ActPassport(**valid_passport)
    assert p.requisites.developers == []                       # паспорта до 2.7 читаются
    p2 = passport_module.apply_user_developers(p, ["Департамент предпринимательства и инновационного развития города Москвы"])
    d = p2.requisites.developers[0]
    assert d.name.startswith("Департамент предпринимательства") and d.basis == "указан пользователем при загрузке"
    assert passport_module.apply_user_developers(p, []) is p


def test_reasoning_comes_before_choice():
    """13.10: модель пишет JSON по порядку полей — сначала обоснование и перечни, потом выбор значения."""
    from app.contracts import AddresseeInfo, Control, StatusInfo
    order = list(Control.model_json_schema()["properties"])
    assert order.index("justification") < order.index("state")
    order = list(StatusInfo.model_json_schema()["properties"])
    assert order.index("status_basis") < order.index("status")
    order = list(AddresseeInfo.model_json_schema()["properties"])
    assert order.index("categories") < order.index("scope") and order.index("distinctions") < order.index("scope")


def test_placeholder_justification_rejected(valid_passport):
    """Скелет ответа с обоснованиями-заглушками («x») контракт не принимает — уходит на повтор."""
    bad = dict(valid_passport, control=dict(valid_passport["control"], justification="x"))
    with pytest.raises(ValidationError):
        ActPassport(**bad)


@pytest.mark.parametrize("block", ["procedure", "control"])
def test_outside_only_scope_contradicts_organ_duties(valid_passport, block):
    """Есть процедура или контроль органа — у органа обязанности по акту: «внешним лицам» — противоречие.
    15.10 (код-ревью): правило — в контракте ответа модели; сохранённый паспорт с таким выбором читается."""
    from app.contracts import ActPassportOutput
    bad = dict(valid_passport)
    bad[block] = dict(valid_passport[block], state="есть", clauses=["п. 3"])
    with pytest.raises(ValidationError, match="внешним лицам"):
        ActPassportOutput(**bad)
    assert ActPassport(**bad).addressee.scope == "внешним лицам"
    bad["addressee"] = dict(valid_passport["addressee"], scope="и тем и другим")
    bad["requisites"] = dict(bad["requisites"], developers_absence_reason="Из текста исполнитель не установлен; названные роли недостаточны.")
    assert ActPassportOutput(**bad).addressee.scope == "и тем и другим"


def test_passport_before_iter4_reads_but_model_answer_rejected():
    """15.10 (код-ревью): настоящий паспорт прода до итерации 4 (1112-ПП: «внешним лицам» при процедуре и контроле
    «есть») читается из файла — диалог по старому прогону работает; от модели такой ответ не принимается."""
    import json
    from pathlib import Path
    from app.contracts import ActPassportOutput
    old = json.loads((Path(__file__).parent / "fixtures" / "passport_before_iter4.json").read_text(encoding="utf-8"))
    assert ActPassport.model_validate(old).addressee.scope == "внешним лицам"
    with pytest.raises(ValidationError, match="внешним лицам"):
        ActPassportOutput.model_validate(old)


def test_passport_without_garant_token_runs_without_garant(run_id, monkeypatch, valid_passport, tmp_path):
    """15.10 (код-ревью): нет файла токена Гаранта — паспорт делается без Гаранта (поиск разработчика — подсказка),
    прогон не падает; в журнале — событие garant_unavailable."""
    import app.runner as runner_mod
    from app.trace import read_events
    monkeypatch.setattr(runner_mod, "GARANT_TOKEN_FILE", tmp_path / "нет_файла")
    mock_call = Mock(return_value=_ok(ActPassport(**valid_passport)))
    monkeypatch.setattr(passport_module, "call", mock_call)
    assert passport_module.make_passport("текст", run_id) is not None
    assert not mock_call.call_args.kwargs.get("mcp_tools")
    assert [e for e in read_events(run_id) if e["event"] == "garant_unavailable"]
    # повторное ревью: описание поля developers велит искать в Гаранте — модели сказано, что его в этот раз нет
    assert "Гаранта в этом прогоне нет" in mock_call.call_args.args[0]
