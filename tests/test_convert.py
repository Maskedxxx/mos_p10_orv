"""
Тесты app/nodes/convert.py (шаг 3, узел [1]).

Реальный прогон docling на файлах-фикстурах, не мок: конвертация быстрая
(~0.5 с на прикaz_dsmi.docx), мок тут только всё запутал бы.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.nodes.convert import convert_to_markdown
from app.trace import run_dir

FIXTURES = Path(__file__).parent / "fixtures"

# Строка-разделитель markdown-таблицы: только "|" и "-" между краевыми "|"
_TABLE_SEPARATOR_RE = re.compile(r"^\|[-|]+\|\s*$", re.MULTILINE)


def test_convert_dsmi_produces_nonempty_markdown_and_saves_artifact(tmp_runs_dir):
    """1. Конвертация приказа ДСМИ даёт непустой markdown, 01_document.md появился в папке прогона."""
    markdown = convert_to_markdown(FIXTURES / "prikaz_dsmi.docx", run_id="run_convert_dsmi")

    assert markdown.strip() != ""
    saved_path = run_dir("run_convert_dsmi") / "01_document.md"
    assert saved_path.exists()
    assert saved_path.read_text(encoding="utf-8") == markdown


def test_convert_dsmi_numbering_preserved(tmp_runs_dir):
    """2. Нумерация пунктов жива — "4.2." находится регекспом в начале строки."""
    markdown = convert_to_markdown(FIXTURES / "prikaz_dsmi.docx", run_id="run_convert_numbering")

    assert re.search(r"^4\.2\.", markdown, re.MULTILINE) is not None


def test_convert_pp1498_tables_preserved(tmp_runs_dir):
    """3. У pp_1498.docx таблицы дошли — в markdown есть markdown-таблицы."""
    markdown = convert_to_markdown(FIXTURES / "pp_1498.docx", run_id="run_convert_tables")

    assert _TABLE_SEPARATOR_RE.search(markdown) is not None


def test_convert_missing_file_raises(tmp_runs_dir):
    """4. Несуществующий файл -> внятная ошибка (FileNotFoundError)."""
    with pytest.raises(FileNotFoundError):
        convert_to_markdown(FIXTURES / "does_not_exist.docx", run_id="run_convert_missing")

def test_convert_rejects_unsupported_format(tmp_path, tmp_runs_dir):
    """Расширение вне списка отвергается до обращения к docling."""
    bad = tmp_path / "акт.txt"
    bad.write_text("текст", encoding="utf-8")
    with pytest.raises(ValueError, match="не поддерживается"):
        convert_to_markdown(bad, run_id="run_convert_bad_format")


def test_convert_pdf_produces_markdown(tmp_runs_dir):
    """PDF читается напрямую через docling, без LibreOffice."""
    pdf = FIXTURES / "act.pdf"
    if not pdf.exists():
        pytest.skip("нет фикстуры act.pdf")
    markdown = convert_to_markdown(pdf, run_id="run_convert_pdf")
    assert markdown.strip()
    assert "ПРАВИТЕЛЬСТВО МОСКВЫ" in markdown.upper() or len(markdown) > 1000

# ======================================================================
# Тесты шва: веб сохраняет файл -> узел конвертации его читает
#
# Раньше этот стык не проверялся. Тесты веба говорили «файл сохранён»,
# тесты конвертации — «файл по пути читается», но связку не гонял никто.
# Баг жил ровно здесь: веб клал любой акт под именем 00_input.docx,
# и PDF уходил в docling с расширением Word.
# ======================================================================


@pytest.mark.parametrize("fixture_name", ["prikaz_dsmi.docx", "act.pdf"])
def test_web_saves_and_convert_reads_same_file(fixture_name, tmp_runs_dir):
    """
    Сквозной путь без моков: имя входного файла собирает веб, читает узел.

    Проверяет не «файл сохранился», а что сохранённое реально открывается
    тем узлом, которому оно предназначено.
    """
    from app import trace
    from app.web import input_filename

    source = FIXTURES / fixture_name
    if not source.exists():
        pytest.skip(f"нет фикстуры {fixture_name}")

    run_id = trace.new_run(fixture_name)
    input_name = input_filename(fixture_name)
    trace.save_bytes(run_id, input_name, source.read_bytes())

    markdown = convert_to_markdown(run_dir(run_id) / input_name, run_id)
    assert markdown.strip(), f"пустой markdown для {fixture_name}"
    assert len(markdown) > 1000, f"подозрительно короткий результат для {fixture_name}"


def test_input_filename_matches_extension_convert_expects():
    """
    Имя от веба несёт расширение, по которому узел выбирает читалку.

    Проверяется литералом, а не константой модуля: тест, сверяющий код
    той же константой, которой код ошибается, не поймает ничего.
    """
    from app.web import input_filename

    assert input_filename("акт.pdf") == "00_input.pdf"
    assert input_filename("акт.docx") == "00_input.docx"
    assert input_filename("акт.doc") == "00_input.doc"
