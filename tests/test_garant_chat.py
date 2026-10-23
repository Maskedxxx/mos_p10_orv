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
from app.garant_chat import GarantFile, GarantTable, GarantTurn
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
    roles = [m["role"] for m in garant_chat.read_chat(chat)]
    assert roles == ["пользователь", "ассистент"]
    assert garant_chat.read_chat(chat)[1]["sources"] == ["Гарант: ПП № 1"]
    session = json.loads((garant_chat.run_dir(garant_chat.chat_run_id(chat)) / garant_chat.SESSION).read_text())
    assert session["session_id"] == "sess-A"
    assert garant_chat.chat_info(chat)["title"] == "Действует ли акт?"
    assert garant_chat.busy_state(chat) is None


def test_turn_with_garant_gives_all_garant_tools(chat, monkeypatch):
    monkeypatch.setattr(garant_chat, "garant_available", lambda: True)
    calls = _fake_calls(monkeypatch, _ok(GarantTurn(reply="ок")))
    garant_chat.turn(chat, "найди акт")
    assert calls[0]["mcp_tools"] == list(garant_chat.GARANT_ALL_TOOLS)
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
    assert last["mark"] == "сбой" and last["text"].startswith("Не получилось ответить")
    assert "Timeout" not in last["text"] and "/home" not in last["text"]


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
    assert garant_chat.read_chat(chat)[-1]["mark"] == "документ"
    assert garant_chat.chat_info(chat)["title"] == "привет"           # название уже было — не меняется


def test_unreadable_document_explained(chat, monkeypatch):
    def broken(path, run_id):
        raise RuntimeError("ConversionError: Conversion failed for: /home/x")

    monkeypatch.setattr(garant_chat, "convert_to_markdown", broken)
    with pytest.raises(ValueError) as error:
        garant_chat.add_document(chat, "скан.pdf", b"%PDF")
    assert "Не удалось прочитать файл «скан.pdf»" in str(error.value) and "/home" not in str(error.value)
    assert garant_chat.chat_info(chat)["documents"] == []


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
    assert garant_chat.read_chat(chat)[-1]["files"] == [{"name": "Справка.docx", "format": "docx"}]
    assert garant_chat.file_path(chat, "Справка.docx") is not None


def test_turn_file_failure_reported(chat, monkeypatch):
    spec = GarantFile(purpose="справка", name="Справка", format="docx", text=MD)
    _fake_calls(monkeypatch, _ok(GarantTurn(reply="Сделал.", files=[spec])))

    def broken(chat_id, spec):
        raise RuntimeError("disk")

    monkeypatch.setattr(garant_chat, "render_file", broken)
    garant_chat.turn(chat, "сделай справку")
    chat_log = garant_chat.read_chat(chat)
    assert chat_log[-2]["role"] == "ассистент" and chat_log[-1]["mark"] == "сбой"
    assert "Справка.docx" in chat_log[-1]["text"]


def test_upload_error_messages():
    from app.garant_page import upload_error

    assert upload_error("акт.docx", 10) is None
    assert "не подходит" in upload_error("акт.txt", 10)
    assert "пустой" in upload_error("акт.pdf", 0)
    assert "больше 20 МБ" in upload_error("акт.pdf", 21 * 1024 * 1024)
