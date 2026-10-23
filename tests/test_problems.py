"""Замечания «Сообщить о проблеме» (app/problems.py, 23.10): запись с номером, раздел по странице, выгрузка в Excel."""
import io

import pytest
from openpyxl import load_workbook

from app import config, problems


@pytest.fixture(autouse=True)
def _tmp_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FEEDBACK_DIR", tmp_path / "feedback")


def test_add_and_read_numbers_in_order():
    assert problems.read_problems() == []
    n1 = problems.add_problem("Результат анализа акта", "  индикатор 3 — нарушен, а в п. 2.4 всё есть ", page="/run/r1",
                              session="s-1", run_id="r1", act="НПА 147-ПП.pdf")
    n2 = problems.add_problem("Гарант-помощник", "не нашёл редакцию", page="/garant/c1", session="s-2", chat_id="c1")
    rows = problems.read_problems()
    assert (n1, n2) == (1, 2) and [r["num"] for r in rows] == [1, 2]
    assert rows[0]["text"] == "индикатор 3 — нарушен, а в п. 2.4 всё есть" and rows[0]["act"] == "НПА 147-ПП.pdf"
    assert problems.where_text(rows[0]) == "НПА 147-ПП.pdf (прогон r1)" and problems.where_text(rows[1]) == "беседа c1"


def test_empty_text_is_refused():
    with pytest.raises(ValueError):
        problems.add_problem("Другое", "   ", page="/", session="s-1")
    assert problems.read_problems() == []


def test_section_by_page():
    assert problems.section_for_path("/run/20261023_141113_f26759") == "Результат анализа акта"
    assert problems.section_for_path("/garant/abc") == "Гарант-помощник"
    assert problems.section_for_path("/") == "Загрузка акта"
    assert problems.section_for_path("/history") == "Другое"


def test_xlsx_has_all_rows():
    problems.add_problem("Другое", "кнопка непонятна", page="/history", session="s-1")
    ws = load_workbook(io.BytesIO(problems.to_xlsx()))["Замечания"]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][:4] == ("№", "Когда", "Где проблема", "Что не так") and rows[1][2:4] == ("Другое", "кнопка непонятна")
