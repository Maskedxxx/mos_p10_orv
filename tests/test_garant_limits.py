"""Тесты app/garant_limits.py — остаток лимитов Гаранта (итерация 4, 14.10)."""

from __future__ import annotations

import io
import json

import pytest

from app import garant_limits


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def api(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("SECRET\n", encoding="utf-8")
    monkeypatch.setattr(garant_limits, "GARANT_TOKEN_FILE", token)
    monkeypatch.setattr(garant_limits, "_cache", None)
    calls = []

    def _open(req, timeout):
        calls.append(req)
        return _Resp(json.dumps([{"title": "Экспорт", "value": 21, "names": ["x"]},
                                 {"title": "Информация о документе", "value": 299}]).encode("utf-8"))
    monkeypatch.setattr(garant_limits._opener, "open", _open)
    return calls


def test_limits_parsed_and_cached(api):
    assert garant_limits.limits() == {"Экспорт": 21, "Информация о документе": 299}
    assert api[0].get_header("Authorization") == "Bearer SECRET"
    garant_limits.limits()
    assert len(api) == 1                       # второй раз — из кэша
    garant_limits.limits(fresh=True)
    assert len(api) == 2                       # fresh — мимо кэша


def test_limits_failure_is_none(api, monkeypatch):
    def _fail(req, timeout):
        raise OSError("нет сети")
    monkeypatch.setattr(garant_limits._opener, "open", _fail)
    assert garant_limits.limits() is None
