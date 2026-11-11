"""
Тесты Гарант-помощника (app/garant_chat.py, app/garant_page.py): беседа, ход агента, память через --resume,
приложенный документ, файлы на скачивание. Вызов модели и шаг чтения замоканы — ни одного живого вызова.
"""

from __future__ import annotations

import json
import shutil

import pytest
from pydantic import ValidationError

from app import garant_chat
from app.config import GARANT_ALL_TOOLS
from app.contracts import GarantFile, GarantTable, GarantTurn
from app.runner import CallResult


def _ok(value: GarantTurn, session_id: str = "sess-A", cost: float = 0.1) -> CallResult:
    return CallResult(value=value, ok=True, attempts=1, cost_usd=cost, session_id=session_id, error=None,
                      permission_denials=[], duration_s=1.0, session_cost_usd=cost)


def _fail(error: str) -> CallResult:
    return CallResult(value=None, ok=False, attempts=3, cost_usd=0.05, session_id=None, error=error,
                      permission_denials=[], duration_s=1.0)


@pytest.fixture
def chat(tmp_runs_dir, monkeypatch):
    """Беседа во временной папке; Гарант недоступен — ход без MCP; лимиты Гаранта не запрашиваются."""
    monkeypatch.setattr(garant_chat, "garant_available", lambda: False)
    monkeypatch.setattr(garant_chat.garant_limits, "limits", lambda fresh=False: None)
    return garant_chat.new_chat("s-test")


def _fake_calls(monkeypatch, *results):
    """Подменить runner.call: отдаёт результаты по очереди и запоминает, с чем его вызвали."""
    calls = []
    queue = list(results)

    def fake(instruction, document, model_cls, **kw):
        calls.append({"instruction": instruction, "document": document, **kw})
        return queue.pop(0)

    monkeypatch.setattr(garant_chat, "call", fake)
    return calls


def test_new_chat_listed_and_id_checked(chat):
    assert garant_chat.valid_chat_id(chat)
    assert not garant_chat.valid_chat_id("../" + chat)
    assert not garant_chat.valid_chat_id("20260101_000000_abcdef")   # формат верный, папки нет
    chats = garant_chat.list_chats()
    assert [c["chat_id"] for c in chats] == [chat]
    assert chats[0]["title"] == "Без названия" and chats[0]["messages"] == 0


def test_turn_new_session_saves_reply_and_session(chat, monkeypatch):
    calls = _fake_calls(monkeypatch, _ok(GarantTurn(reply="Акт действует.", sources=["Гарант: ПП № 1"])))
    result = garant_chat.turn(chat, "Действует ли акт?", session="s-test")
    assert result.ok and result.reply == "Акт действует."
    call = calls[0]
    assert call["trace_name"] == "turns/0001.raw.json" and "resume_session_id" not in call
    assert call["mcp_tools"] is None                                   # Гарант недоступен — без MCP
    assert "Гарант сейчас недоступен" in call["document"] and "Действует ли акт?" in call["document"]
    roles = [m.role for m in garant_chat.read_chat(chat)]
    assert roles == ["пользователь", "ассистент"]
    assert garant_chat.read_chat(chat)[1].sources == ["Гарант: ПП № 1"]
    session = json.loads((garant_chat.run_dir(garant_chat.chat_run_id(chat)) / garant_chat.SESSION).read_text())
    assert session["session_id"] == "sess-A"
    assert garant_chat.chat_info(chat).title == "Действует ли акт?"
    assert garant_chat.busy_state(chat) is None


def test_turn_with_garant_gives_all_garant_tools(chat, monkeypatch):
    monkeypatch.setattr(garant_chat, "garant_available", lambda: True)
    calls = _fake_calls(monkeypatch, _ok(GarantTurn(reply="ок")))
    garant_chat.turn(chat, "найди акт")
    assert calls[0]["mcp_tools"] == list(GARANT_ALL_TOOLS)
    assert "СЛУЖЕБНО" not in calls[0]["document"]


def test_second_turn_resumes_session(chat, monkeypatch):
    calls = _fake_calls(monkeypatch, _ok(GarantTurn(reply="один")), _ok(GarantTurn(reply="два"), cost=0.3))
    garant_chat.turn(chat, "первый")
    garant_chat.turn(chat, "второй")
    second = calls[1]
    assert second["resume_session_id"] == "sess-A" and second["document"] is None
    assert second["trace_name"] == "turns/0002.raw.json"
    assert second["resume_cost_base"] == pytest.approx(0.1)


def test_lost_session_starts_new_with_history(chat, monkeypatch):
    calls = _fake_calls(monkeypatch, _ok(GarantTurn(reply="один")),
                        _fail("No conversation found with session ID sess-A"),
                        _ok(GarantTurn(reply="два"), session_id="sess-B"))
    garant_chat.turn(chat, "первый")
    result = garant_chat.turn(chat, "второй")
    assert result.ok and result.reply == "два"
    assert calls[2]["trace_name"] == "turns/0002.new.raw.json"
    assert "первый" in calls[2]["document"] and "второй" in calls[2]["document"]   # история — во входе
    session = json.loads((garant_chat.run_dir(garant_chat.chat_run_id(chat)) / garant_chat.SESSION).read_text())
    assert session["session_id"] == "sess-B"


def test_failed_turn_is_understandable(chat, monkeypatch):
    _fake_calls(monkeypatch, _fail("Timeout after 900s: /home/masked/projects/…"))
    result = garant_chat.turn(chat, "вопрос")
    assert not result.ok
    last = garant_chat.read_chat(chat)[-1]
    assert last.mark == "сбой" and last.text.startswith("Не получилось ответить")
    assert "Timeout" not in last.text and "/home" not in last.text


def test_document_resets_session_and_goes_to_context(chat, monkeypatch):
    _fake_calls(monkeypatch, _ok(GarantTurn(reply="ок")))
    garant_chat.turn(chat, "привет")
    root = garant_chat.run_dir(garant_chat.chat_run_id(chat))
    assert (root / garant_chat.SESSION).is_file()

    def fake_convert(path, run_id):
        (garant_chat.run_dir(run_id) / "01_document.md").write_text("x", encoding="utf-8")
        return "ТЕКСТ ПОСТАНОВЛЕНИЯ п. 1"

    monkeypatch.setattr(garant_chat, "convert_to_markdown", fake_convert)
    chars = garant_chat.add_document(chat, "Постановление 166-ПП.docx", b"PK..")
    assert chars == len("ТЕКСТ ПОСТАНОВЛЕНИЯ п. 1")
    assert not (root / garant_chat.SESSION).exists()                  # следующий ход — новая сессия
    assert not (root / "01_document.md").exists()
    assert "ТЕКСТ ПОСТАНОВЛЕНИЯ п. 1" in garant_chat.shared_context(chat)
    assert garant_chat.read_chat(chat)[-1].mark == "документ"
    assert garant_chat.chat_info(chat).title == "привет"           # название уже было — не меняется


def test_unreadable_document_explained(chat, monkeypatch):
    def broken(path, run_id):
        raise RuntimeError("ConversionError: Conversion failed for: /home/x")

    monkeypatch.setattr(garant_chat, "convert_to_markdown", broken)
    with pytest.raises(ValueError) as error:
        garant_chat.add_document(chat, "скан.pdf", b"%PDF")
    assert "Не удалось прочитать файл «скан.pdf»" in str(error.value) and "/home" not in str(error.value)
    assert garant_chat.chat_info(chat).documents == []


def test_delete_one_document_preserves_other_and_starts_new_session(chat, monkeypatch):
    monkeypatch.setattr(garant_chat, "convert_to_markdown", lambda path, rid: path.read_bytes().decode())
    calls = _fake_calls(monkeypatch, _ok(GarantTurn(reply="Ответ до удаления")), _ok(GarantTurn(reply="После удаления")))
    garant_chat.add_document(chat, "первый.docx", b"FIRST TEXT")
    garant_chat.add_document(chat, "второй.pdf", b"SECOND TEXT")
    garant_chat.turn(chat, "Сохранить историю")
    root = garant_chat.run_dir(garant_chat.chat_run_id(chat))
    assert (root / garant_chat.SESSION).exists()
    garant_chat.remove_document(chat, 1)
    assert [(d.n, d.name) for d in garant_chat.chat_info(chat).documents] == [(2, "второй.pdf")]
    assert not (root / "docs/01.docx").exists() and not (root / "docs/01.md").exists()
    assert (root / "docs/02.pdf").exists() and (root / "docs/02.md").exists()
    assert not (root / garant_chat.SESSION).exists()
    assert "FIRST TEXT" not in garant_chat.shared_context(chat)
    assert "SECOND TEXT" in garant_chat.shared_context(chat)
    garant_chat.turn(chat, "Следующий вопрос")
    assert "resume_session_id" not in calls[-1]
    assert "FIRST TEXT" not in calls[-1]["shared_context"]
    assert "SECOND TEXT" in calls[-1]["shared_context"]
    assert "Сохранить историю" in calls[-1]["document"]
    garant_chat.add_document(chat, "третий.docx", b"THIRD TEXT")
    assert [d.n for d in garant_chat.chat_info(chat).documents] == [2, 3]
    garant_chat.remove_document(chat, 2)
    garant_chat.remove_document(chat, 3)
    garant_chat.add_document(chat, "четвёртый.docx", b"FOURTH TEXT")
    assert [d.n for d in garant_chat.chat_info(chat).documents] == [4]


def test_busy_or_missing_document_does_not_remove_other_files(chat, monkeypatch):
    monkeypatch.setattr(garant_chat, "convert_to_markdown", lambda path, rid: path.read_bytes().decode())
    garant_chat.add_document(chat, "первый.docx", b"first")
    garant_chat.add_document(chat, "второй.docx", b"second")
    root = garant_chat.run_dir(garant_chat.chat_run_id(chat))
    garant_chat._set_busy(chat, "ищет")
    try:
        with pytest.raises(ValueError, match="уже выполняется"):
            garant_chat.remove_document(chat, 1)
    finally:
        garant_chat._set_busy(chat, None)
    with pytest.raises(ValueError, match="не найден"):
        garant_chat.remove_document(chat, 99)
    assert [d.n for d in garant_chat.chat_info(chat).documents] == [1, 2]
    assert (root / "docs/01.md").read_text() == "first"
    assert (root / "docs/02.md").read_text() == "second"


def test_delete_failure_restores_selected_and_leaves_other(chat, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(garant_chat, "convert_to_markdown", lambda path, rid: path.read_bytes().decode())
    garant_chat.add_document(chat, "первый.docx", b"first")
    garant_chat.add_document(chat, "второй.docx", b"second")
    root = garant_chat.run_dir(garant_chat.chat_run_id(chat))
    original_unlink = Path.unlink
    def fail_selected_text(path, *args, **kwargs):
        if path == root / "docs/01.md":
            raise OSError("test failure")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_selected_text)
    with pytest.raises(ValueError, match="Не удалось удалить"):
        garant_chat.remove_document(chat, 1)
    assert [d.n for d in garant_chat.chat_info(chat).documents] == [1, 2]
    assert (root / "docs/01.docx").read_bytes() == b"first"
    assert (root / "docs/02.docx").read_bytes() == b"second"
    assert "first" in garant_chat.shared_context(chat) and "second" in garant_chat.shared_context(chat)


def test_file_contract_requires_matching_content():
    with pytest.raises(ValidationError):
        GarantFile(purpose="таблица", name="Т", format="xlsx", text="| a |")
    with pytest.raises(ValidationError):
        GarantFile(purpose="справка", name="С", format="docx", text="   ")
    GarantFile(purpose="таблица", name="Т", format="xlsx", table=GarantTable(headers=["a"], rows=[["1"]]))


MD = "# Справка\n\nАкт **действует**.\n\n- пункт один\n1. шаг\n\n| Акт | Статус |\n|---|---|\n| ПП № 1 | действует |\n"


def test_render_docx_md_xlsx(chat):
    from docx import Document
    from openpyxl import load_workbook

    docx = garant_chat.render_file(chat, GarantFile(purpose="справка", name="Справка/../по акту", format="docx",
                                                    text=MD))
    path = garant_chat.file_path(chat, docx["name"])
    assert path is not None and docx["name"] == "Справка..по акту.docx"
    doc = Document(str(path))
    texts = [p.text for p in doc.paragraphs]
    assert "Справка" in texts and "Акт действует." in texts and "пункт один" in texts
    assert doc.tables[0].cell(1, 1).text == "действует"

    again = garant_chat.render_file(chat, GarantFile(purpose="справка", name="Справка/../по акту", format="docx",
                                                     text="текст"))
    assert again["name"] == "Справка..по акту (2).docx"

    md = garant_chat.render_file(chat, GarantFile(purpose="текст", name="Заметка", format="md", text=MD))
    assert garant_chat.file_path(chat, md["name"]).read_text(encoding="utf-8") == MD

    xlsx = garant_chat.render_file(chat, GarantFile(purpose="таблица", name="Редакции", format="xlsx",
                                                    table=GarantTable(headers=["Редакция", "Дата"],
                                                                      rows=[["1", "01.01.2025"]])))
    ws = load_workbook(garant_chat.file_path(chat, xlsx["name"])).active
    assert [c.value for c in ws[1]] == ["Редакция", "Дата"] and ws["B2"].value == "01.01.2025"


@pytest.mark.skipif(shutil.which("soffice") is None, reason="нет LibreOffice")
def test_render_pdf(chat):
    pdf = garant_chat.render_file(chat, GarantFile(purpose="pdf", name="Справка", format="pdf", text=MD))
    assert garant_chat.file_path(chat, pdf["name"]).read_bytes()[:4] == b"%PDF"


def test_file_path_stays_inside_files(chat):
    garant_chat.render_file(chat, GarantFile(purpose="текст", name="А", format="md", text="а"))
    assert garant_chat.file_path(chat, "А.md") is not None
    assert garant_chat.file_path(chat, "../chat.json") is None
    assert garant_chat.file_path(chat, "нет.md") is None


def test_turn_files_made_and_listed(chat, monkeypatch):
    spec = GarantFile(purpose="справка", name="Справка", format="docx", text=MD)
    _fake_calls(monkeypatch, _ok(GarantTurn(reply="Сделал справку.", files=[spec])))
    result = garant_chat.turn(chat, "сделай справку")
    assert result.files == [{"name": "Справка.docx", "format": "docx"}]
    assert [f.model_dump() for f in garant_chat.read_chat(chat)[-1].files] == [{"name": "Справка.docx", "format": "docx"}]
    assert garant_chat.file_path(chat, "Справка.docx") is not None


def test_turn_file_failure_reported(chat, monkeypatch):
    spec = GarantFile(purpose="справка", name="Справка", format="docx", text=MD)
    _fake_calls(monkeypatch, _ok(GarantTurn(reply="Сделал.", files=[spec])))

    def broken(chat_id, spec):
        raise RuntimeError("disk")

    monkeypatch.setattr(garant_chat, "render_file", broken)
    garant_chat.turn(chat, "сделай справку")
    chat_log = garant_chat.read_chat(chat)
    assert chat_log[-2].role == "ассистент" and chat_log[-1].mark == "сбой"
    assert "Справка.docx" in chat_log[-1].text


def test_upload_error_messages():
    from app.garant_page import upload_error

    assert upload_error("акт.docx", 10) is None
    assert "не подходит" in upload_error("акт.txt", 10)
    assert "пустой" in upload_error("акт.pdf", 0)
    assert "больше 20 МБ" in upload_error("акт.pdf", 21 * 1024 * 1024)
