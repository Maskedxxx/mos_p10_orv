"""
Общие фикстуры для тестов сервиса П-10 (шаг 1).

Содержит: подмену RUNS_DIR на временную папку для изоляции тестов трейсов
и логера, а также фабрики минимально валидных данных контрактов — чтобы не
дублировать одинаковые dict-заготовки по разным тестовым файлам.
"""

from __future__ import annotations

import pytest

import app.logger as logger_module
import app.trace as trace_module


@pytest.fixture
def tmp_runs_dir(tmp_path, monkeypatch):
    """Подменить RUNS_DIR на временную папку для изоляции тестов трейсов и логера."""
    monkeypatch.setattr(trace_module, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(logger_module, "RUNS_DIR", tmp_path)
    return tmp_path


# ---- Фабрики минимально валидных данных контрактов ----


@pytest.fixture
def valid_effect() -> dict:
    return {"affected": "бизнес", "metric": "количество обращений", "how_to_calculate": "сумма за год"}


@pytest.fixture
def valid_data_item() -> dict:
    return {"metric": "число заявок", "source": "открытый", "where": "Мосстат"}


@pytest.fixture
def valid_verdict_ok() -> dict:
    """Вердикт со статусом, отличным от "нет данных" (missing_data отсутствует)."""
    return {
        "indicator": 1,
        "name": "межведомственное взаимодействие",
        "verdict": "соблюдён",
        "rationale": "требование реализовано через СМЭВ",
    }


@pytest.fixture
def valid_verdict_no_data() -> dict:
    """Вердикт со статусом "нет данных" (missing_data обязателен)."""
    return {
        "indicator": 10,
        "name": "учёт практики применения",
        "verdict": "нет данных",
        "rationale": "нет статистики по обращениям",
        "missing_data": "количество обращений за 3 года",
    }


@pytest.fixture
def valid_hypothesis(valid_effect, valid_data_item) -> dict:
    return {
        "hypothesis": "требование избыточно",
        "grounds": "требование избыточно — из карточки вердикта",
        "effects": [valid_effect],
        "data": [valid_data_item],
    }


@pytest.fixture
def valid_hypothesis_set(valid_hypothesis) -> dict:
    return {
        "indicator": 7,
        "name": "отсутствие избыточных процедур",
        "violation_kind": "издержки",
        "why_this_many": "выявлено одно нарушение",
        "hypotheses": [valid_hypothesis],
    }


@pytest.fixture
def valid_map_row(valid_effect, valid_data_item) -> dict:
    return {
        "row_id": 1,
        "indicators": [7],
        "hypothesis": "требование избыточно",
        "grounds": "требование избыточно — из карточки вердикта",
        "effects": [valid_effect],
        "data": [valid_data_item],
    }


@pytest.fixture
def valid_merge() -> dict:
    return {"merged_row_ids": [1, 2], "reason": "дублирующиеся гипотезы"}


@pytest.fixture
def valid_request_item() -> dict:
    return {
        "num": "1", "kind": "показатель по годам", "metric": "число обращений (пункт 1)",
        "unit": "ед.", "for_rows": [1], "sources": [{"row_id": 1, "metric": "число обращений"}],
    }


@pytest.fixture
def valid_run_meta() -> dict:
    return {
        "run_id": "20260101_120000_abc123",
        "act_filename": "act.docx",
        "started_at": "2026-01-01T12:00:00",
        "status": "started",
    }


@pytest.fixture
def valid_passport() -> dict:
    """Минимальный валидный паспорт акта (ActPassport): все объекты «нет» с обоснованием."""
    stated_no = {"state": "нет", "justification": "искал по всему тексту, не установлено"}
    return {
        "requisites": {"kind": "приказ", "number": "N 1", "date": "1 января 2024 г.",
                       "authority": "Департамент", "title": "Об утверждении порядка"},
        "status": {"status": "принят", "status_basis": "есть номер, дата, подпись",
                   "effective_date": None, "effective_date_clause": None, "deferred": [], "repealed_clauses": []},
        "addressee": {"scope": "внешним лицам", "categories": [{"name": "перевозчики", "clause": "п. 1"}],
                      "distinctions": []},
        "nature": {"actions": [{"action": "утверждает новое", "target": "Порядок", "clauses": ["п. 1"]}],
                   "issued_under": [], "input_kind": "акт изменений не вносит"},
        "requirements": {**stated_no, "groups": []},
        "procedure": {**stated_no, "participants": None, "result": None, "has_document_check": None,
                      "refusal_grounds": None, "clauses": []},
        "documents": {**stated_no, "items": [], "transfer_clauses": []},
        "deadlines": {**stated_no, "items": []},
        "references": {**stated_no, "acts": [], "authorities": []},
        "allocation": {"competitive": {**stated_no, "kind": None, "resource": None, "clauses": []},
                       "appointed": {**stated_no, "items": []}},
        "control": {**stated_no, "authority": None, "subject": None, "measures": [], "outcomes": [], "clauses": []},
    }
