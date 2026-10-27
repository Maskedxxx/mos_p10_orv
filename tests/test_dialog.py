"""
Тесты app/dialog.py — один ход диалога с результатом прогона (план итерации 3, шаг 4).
runner.call подменяется: проверяется сборка входа, журнал чата, учёт денег.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import app.dialog as dialog_module
from app.contracts import DialogTurn, RunMeta, SetVerdict
from app.runner import CallResult
from app.trace import load_json, read_events, run_dir, save_json
from test_versions import _write_run


@pytest.fixture
def rid(tmp_runs_dir, tmp_path, monkeypatch) -> str:
    rid = "run_dialog"
    _write_run(rid)
    save_json(rid, "meta.json", RunMeta(run_id=rid, act_filename="act.docx", started_at="2026-09-07T09:00:00",
                                        finished_at="2026-09-07T09:15:00", status="finished", cost_usd=10.0))
    (run_dir(rid) / "01_document.md").write_text("ТЕКСТ АКТА: п. 2.4.1 ежедневный осмотр", encoding="utf-8")
    prompt = tmp_path / "07_dialog.md"
    prompt.write_text("Промпт 07_dialog (фикстура).", encoding="utf-8")
    monkeypatch.setattr(dialog_module, "PROMPT_DIALOG", prompt)
    return rid


def _ok(value: DialogTurn, cost: float = 0.4) -> CallResult:
    return CallResult(value=value, ok=True, attempts=1, cost_usd=cost, session_id="s", error=None,
                      permission_denials=[], duration_s=1.0)


def test_turn_sends_all_blocks_and_records_chat(rid, monkeypatch):
    """Первый ход — новая сессия: системный промпт = промпт 07 + карта прогона + акт + паспорт;
    вход = служебная строка, история, сообщение; инструменты — чтение артефактов прогона (14.10)."""
    seen = {}

    def _effect(instruction, document, model_cls, **kw):
        seen.update(instruction=instruction, document=document, model_cls=model_cls, **kw)
        return _ok(DialogTurn(reply="Индикатор 3 нарушен, потому что…"))

    monkeypatch.setattr(dialog_module, "call", Mock(side_effect=_effect))
    dialog_module.append_chat(rid, "сервис", "применено: версия 1")

    res = dialog_module.turn(rid, "Почему индикатор 3 нарушен?")

    system, doc = seen["shared_context"], seen["document"]
    assert system.startswith("Промпт 07_dialog (фикстура).")
    for block in ("# КАРТА ПРОГОНА", "## ПОЛНЫЙ ТЕКСТ АКТА", "ТЕКСТ АКТА: п. 2.4.1"):
        assert block in system
    for block in ("## СЛУЖЕБНО", "Сейчас версия v0.", "## ИСТОРИЯ ДИАЛОГА", "## СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ"):
        assert block in doc
    assert "## ВЕРДИКТЫ (JSON)" not in doc + system          # вердикты помощник читает инструментом
    assert doc.rstrip().endswith("Почему индикатор 3 нарушен?") and "применено: версия 1" in doc
    assert seen["instruction"] == dialog_module.NEW_SESSION_INSTRUCTION and "resume_session_id" not in seen
    assert seen["model_cls"] is DialogTurn and seen["allowed_tools"] == ["WebSearch", "WebFetch"]
    # решение владельца 14.10: весь Гарант всегда (и выгрузки) + чтение артефактов
    from app.config import GARANT_ALL_TOOLS
    assert seen["mcp_tools"] == list(GARANT_ALL_TOOLS) + dialog_module.RUN_TOOLS
    assert "garant_export_block_html" in seen["mcp_tools"]
    assert seen["model"] == dialog_module.DIALOG_MODEL and seen["short_cache_ttl"] is False
    assert seen["trace_name"].startswith("dialog/turns/")
    assert res.reply.startswith("Индикатор 3") and res.plan == []
    roles = [m["role"] for m in dialog_module.read_chat(rid)]
    assert roles == ["сервис", "пользователь", "ассистент"]
    assert load_json(rid, "meta.json", RunMeta).cost_usd == pytest.approx(10.4)
    sess = dialog_module.load_session(rid)
    assert sess["session_id"] == "s" and sess["version_seen"] == 0


def test_turn_returns_plan(rid, monkeypatch):
    op = SetVerdict(kind="смена вердикта", indicator=7, verdict="нарушен", rationale="15 форм", reason="эксперт")
    monkeypatch.setattr(dialog_module, "call", Mock(return_value=_ok(DialogTurn(reply="Сменю вердикт.", plan=[op]))))
    res = dialog_module.turn(rid, "Эксперт: инд. 7 не соблюдён")
    assert res.plan == [op]
    assert dialog_module.read_chat(rid)[-1]["plan"][0]["kind"] == "смена вердикта"


def test_turn_failure_gives_readable_reply_without_plan(rid, monkeypatch):
    failed = CallResult(value=None, ok=False, attempts=3, cost_usd=0.1, session_id=None, error="таймаут",
                        permission_denials=[], duration_s=1.0)
    monkeypatch.setattr(dialog_module, "call", Mock(return_value=failed))
    res = dialog_module.turn(rid, "Что с индикатором 3?")
    assert res.plan == [] and "не получилось" in res.reply.lower()
    assert dialog_module.read_chat(rid)[-1]["mark"] == "сбой"


def test_remarks_queue_one_by_one(rid):
    from app.contracts import Remark
    rs = [Remark(sheet="Вердикты", key="1", title="т", agree="нет"), Remark(sheet="Карта гипотез", key="2", title="г", text="x")]
    assert dialog_module.remarks_progress(rid) is None
    dialog_module.save_remarks(rid, "эксперт.xlsx", rs)
    assert dialog_module.remarks_progress(rid) == (0, 2)
    assert dialog_module.next_remark(rid) == rs[0]
    assert dialog_module.next_remark(rid) == rs[1]
    assert dialog_module.next_remark(rid) is None and dialog_module.remarks_progress(rid) == (2, 2)


def test_status_file_set_read_clear(rid):
    assert dialog_module.read_status(rid) is None
    dialog_module.set_status(rid, "шаг", "Выполняю шаг 1 из 1", "обычно несколько секунд")
    st = dialog_module.read_status(rid)
    assert st["state"] == "шаг" and st["server"] == dialog_module.SERVER_ID and st["since"]
    dialog_module.clear_status(rid)
    assert dialog_module.read_status(rid) is None


def test_turn_marks_user_message_with_session(rid, monkeypatch):
    monkeypatch.setattr(dialog_module, "call", Mock(return_value=_ok(DialogTurn(reply="Ответ."))))
    dialog_module.turn(rid, "Вопрос", session="s-7f3a91")
    user, assistant = dialog_module.read_chat(rid)
    assert user["session"] == "s-7f3a91" and "session" not in assistant


def test_mcp_config_reads_only_this_run(rid):
    """Конфиг серверов помощника: Гарант как в пайплайне и чтение артефактов именно этого прогона."""
    import json
    data = json.loads(open(dialog_module.mcp_config(rid), encoding="utf-8").read())["mcpServers"]
    assert data["garant"]["headers"]["Authorization"] == "Bearer ${GARANT_TOKEN}"
    assert data["p10run"]["args"][0].endswith("app/mcp_run.py")
    assert data["p10run"]["env"]["P10_RUN_DIR"] == str(run_dir(rid).resolve())


def test_second_turn_resumes_with_version_line(rid, monkeypatch):
    """Второй ход — --resume той же сессии: только служебная строка и сообщение; после правки строка
    называет новую версию и что изменилось."""
    from app import versions
    calls = []

    def _effect(instruction, document, model_cls, **kw):
        calls.append(dict(instruction=instruction, document=document, **kw))
        return CallResult(value=DialogTurn(reply="ок"), ok=True, attempts=1, cost_usd=0.1, session_id="sess-A",
                          error=None, permission_denials=[], duration_s=1.0)

    monkeypatch.setattr(dialog_module, "call", Mock(side_effect=_effect))
    dialog_module.turn(rid, "первый вопрос")
    dialog_module.turn(rid, "второй вопрос")
    assert calls[1]["resume_session_id"] == "sess-A" and calls[1]["document"] is None
    assert "Сейчас версия v0. С прошлого хода изменений нет." in calls[1]["instruction"]
    assert calls[1]["instruction"].rstrip().endswith("второй вопрос") and "ИСТОРИЯ" not in calls[1]["instruction"]
    # правка между ходами — новая версия и строка журнала
    from test_versions import _entry
    versions.commit(rid, versions.load_state(rid), [_entry(1)])
    dialog_module.turn(rid, "третий вопрос")
    assert "Сейчас версия v1. С прошлого хода изменено: v1: переформулировано — Карта гипотез, стр. 1." in calls[2]["instruction"]
    assert dialog_module.load_session(rid)["version_seen"] == 1
    events = [e["event"] for e in __import__("app.trace", fromlist=["read_events"]).read_events(rid)]
    assert events.count("dialog_session_started") == 1 and events.count("dialog_session_resumed") == 2
    assert events.count("dialog_turn") == 3


def test_lost_session_falls_back_to_new_with_history(rid, monkeypatch):
    """Продолжить сессию не удалось — тот же ход идёт новой сессией с историей чата; стоимость — обе попытки."""
    failed = CallResult(value=None, ok=False, attempts=3, cost_usd=0.05, session_id=None,
                        error="процесс завершился с кодом 1: No conversation found with session ID: dead",
                        permission_denials=[], duration_s=1.0)   # так CLI отвечает на --resume без сессии (проба 15.10)
    new = CallResult(value=DialogTurn(reply="ок"), ok=True, attempts=1, cost_usd=0.2, session_id="sess-B",
                     error=None, permission_denials=[], duration_s=1.0)
    dialog_module._save_session(rid, {"session_id": "dead", "started": "x", "last_turn": "x", "version_seen": 0})
    mock = Mock(side_effect=[failed, new])
    monkeypatch.setattr(dialog_module, "call", mock)
    res = dialog_module.turn(rid, "вопрос")
    assert res.reply == "ок" and res.cost_usd == pytest.approx(0.25)
    assert mock.call_args_list[0].kwargs["resume_session_id"] == "dead"
    assert "resume_session_id" not in mock.call_args_list[1].kwargs and "## ИСТОРИЯ ДИАЛОГА" in mock.call_args_list[1].args[1]
    assert dialog_module.load_session(rid)["session_id"] == "sess-B"


def _write_turn_stream(rid, n: int, tool: str) -> None:
    """Файл хода помощника (его пишет runner): один вызов инструмента."""
    import json
    path = run_dir(rid) / "dialog" / "turns" / f"{n:04d}.stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": "t", "attempt": 1, "event": {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "e1", "name": tool, "input": {"topic": 1}}]}}}) + "\n", encoding="utf-8")


def _exports_logged(rid: str) -> list[dict]:
    return [e for e in read_events(rid) if e["event"] == "garant_exports_spent"]


def test_exports_logged_only_when_spent(rid, monkeypatch):
    """Выгрузки помощник делает сам; потратил — запись «потрачено N» в журнале прогона, не тратил — записи нет. 15.10: «потрачено» — по удачным вызовам выгрузки самого помощника в этом ходе. 27.10: в чат эксперту строка
    не пишется — его план остаётся последней репликой и ждёт решения (web.pending_plan)."""
    op = SetVerdict(kind="смена вердикта", indicator=7, verdict="нарушен", rationale="15 форм", reason="эксперт")

    def _effect(*a, **kw):
        _write_turn_stream(rid, 1, "mcp__garant__garant_export_block_html")
        return _ok(DialogTurn(reply="ок", plan=[op]))
    monkeypatch.setattr(dialog_module, "call", Mock(side_effect=_effect))
    dialog_module.turn(rid, "выгрузи п. 5")
    chat = dialog_module.read_chat(rid)
    assert [e["role"] for e in chat] == ["пользователь", "ассистент"]
    assert chat[-1]["plan"][0]["kind"] == "смена вердикта"
    assert [e["spent"] for e in _exports_logged(rid)] == [1]
    monkeypatch.setattr(dialog_module, "call", Mock(return_value=_ok(DialogTurn(reply="ок"))))
    dialog_module.turn(rid, "а теперь вопрос")
    assert dialog_module.read_chat(rid)[-1]["role"] == "ассистент"
    assert len(_exports_logged(rid)) == 1


def test_exports_of_parallel_run_not_attributed_to_assistant(rid, monkeypatch):
    """15.10 (код-ревью): остаток выгрузок общий — пока помощник отвечал, выгрузку потратил параллельный прогон;
    помощник ничего не выгружал — записи «потрачено» нет."""
    monkeypatch.setattr(dialog_module, "call", Mock(return_value=_ok(DialogTurn(reply="ок"))))
    dialog_module.turn(rid, "вопрос")
    assert dialog_module.read_chat(rid)[-1]["role"] == "ассистент"
    assert _exports_logged(rid) == []


def test_temporary_resume_failure_keeps_session(rid, monkeypatch):
    """15.10 (код-ревью): сбой продолжения разговора (таймаут, ошибка API) — память помощника не стирается и новая
    сессия не запускается: ход — «не получилось», следующий ход продолжит ту же сессию."""
    failed = CallResult(value=None, ok=False, attempts=3, cost_usd=0.05, session_id=None,
                        error="процесс завершился с кодом 1: таймаут", permission_denials=[], duration_s=1.0)
    dialog_module._save_session(rid, {"session_id": "alive", "started": "x", "last_turn": "x", "version_seen": 0})
    mock = Mock(return_value=failed)
    monkeypatch.setattr(dialog_module, "call", mock)
    res = dialog_module.turn(rid, "вопрос")
    assert mock.call_count == 1 and "не получилось" in res.reply.lower()
    assert dialog_module.load_session(rid)["session_id"] == "alive"


def test_turn_without_garant_token_works_without_garant(rid, monkeypatch, tmp_path):
    """15.10 (код-ревью): нет файла токена Гаранта — помощник отвечает без Гаранта (только файлы прогона и интернет),
    ход не падает. Повторное ревью: предупреждение — служебной строкой во входе модели (помощник скажет сам), а не
    репликой чата: та сбивала номер хода для живых шагов и засоряла историю разговора."""
    import app.runner as runner_mod
    monkeypatch.setattr(runner_mod, "GARANT_TOKEN_FILE", tmp_path / "нет_файла")
    mock = Mock(return_value=_ok(DialogTurn(reply="ок")))
    monkeypatch.setattr(dialog_module, "call", mock)
    dialog_module.turn(rid, "вопрос")
    kw = mock.call_args.kwargs
    assert kw["mcp_tools"] == dialog_module.RUN_TOOLS
    assert "garant" not in __import__("json").loads(open(kw["mcp_config"], encoding="utf-8").read())["mcpServers"]
    assert "Гарант сейчас недоступен" in mock.call_args.args[1]
    assert [e["role"] for e in dialog_module.read_chat(rid)] == ["пользователь", "ассистент"]


def test_turn_steps_from_stream(rid):
    """Шаги хода — из файла хода: вызовы инструментов по порядку, без дублей и служебного ответа по схеме."""
    import json
    path = run_dir(rid) / "dialog" / "turns" / "0003.stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    def tool(i, name, args):
        return json.dumps({"ts": "t", "attempt": 1, "event": {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": i, "name": name, "input": args}]}}}, ensure_ascii=False)
    lines = [tool("a", "mcp__p10run__read_artifact", {"path": "03_verdicts/03.json"}),
             tool("a", "mcp__p10run__read_artifact", {"path": "03_verdicts/03.json"}),          # дубль события
             json.dumps({"ts": "t", "event": {"type": "stream_event"}}), "не json",
             tool("b", "mcp__garant__garant_search", {"query": "Number(166-ПП)"}),
             tool("c", "WebFetch", {"url": "https://mos.ru/x"}),
             tool("d", "StructuredOutput", {"reply": "…"})]
    path.write_text("\n".join(lines), encoding="utf-8")
    assert dialog_module.turn_steps(rid, 3) == ["читаю 03_verdicts/03.json", "ищу в Гаранте: Number(166-ПП)",
                                                 "открываю https://mos.ru/x"]
    assert dialog_module.turn_steps(rid, 5) == []


def test_resume_failing_two_turns_in_a_row_starts_new_session(rid, monkeypatch):
    """15.10 (повторное ревью): сессия, которую не удаётся продолжить второй ход подряд (длинная история, сбой
    истории), — сломана: второй ход идёт новой сессией с историей чата, а не «не получилось» навсегда."""
    failed = CallResult(value=None, ok=False, attempts=3, cost_usd=0.05, session_id=None,
                        error="ошибка API: Prompt is too long", permission_denials=[], duration_s=1.0)
    new = CallResult(value=DialogTurn(reply="ок"), ok=True, attempts=1, cost_usd=0.2, session_id="sess-B",
                     error=None, permission_denials=[], duration_s=1.0)
    dialog_module._save_session(rid, {"session_id": "stuck", "started": "x", "last_turn": "x", "version_seen": 0})
    mock = Mock(side_effect=[failed, failed, new])
    monkeypatch.setattr(dialog_module, "call", mock)
    assert "не получилось" in dialog_module.turn(rid, "первый").reply.lower()
    assert dialog_module.load_session(rid)["session_id"] == "stuck"
    assert dialog_module.turn(rid, "второй").reply == "ок"
    assert mock.call_count == 3 and dialog_module.load_session(rid)["session_id"] == "sess-B"


def test_failed_resume_updates_cost_base(rid, monkeypatch):
    """15.10 (повторное ревью): CLI сообщает стоимость сессии нарастающим итогом; после неудачного продолжения база
    обновляется — иначе следующий ход ещё раз посчитает деньги упавшего хода."""
    failed = CallResult(value=None, ok=False, attempts=3, cost_usd=0.9, session_id="alive", error="ответ не по схеме",
                        permission_denials=[], duration_s=1.0, session_cost_usd=1.9)
    dialog_module._save_session(rid, {"session_id": "alive", "started": "x", "last_turn": "x", "version_seen": 0,
                                      "cost_reported": 1.0})
    monkeypatch.setattr(dialog_module, "call", Mock(return_value=failed))
    dialog_module.turn(rid, "вопрос")
    assert dialog_module.load_session(rid)["cost_reported"] == 1.9


def test_failed_export_call_not_counted(rid, monkeypatch):
    """15.10 (повторное ревью): вызов выгрузки, на который Гарант ответил ошибкой (лимит, нет документа), выгрузку
    не тратит — записи «потрачено» нет."""
    import json

    def _effect(*a, **kw):
        _write_turn_stream(rid, 1, "mcp__garant__garant_export_block_html")
        with open(run_dir(rid) / "dialog" / "turns" / "0001.stream.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "t", "attempt": 1, "event": {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "e1", "is_error": True,
                 "content": [{"type": "text", "text": "лимит исчерпан"}]}]}}}) + "\n")
        return _ok(DialogTurn(reply="ок"))
    monkeypatch.setattr(dialog_module, "call", Mock(side_effect=_effect))
    dialog_module.turn(rid, "выгрузи п. 5")
    assert [e["role"] for e in dialog_module.read_chat(rid)] == ["пользователь", "ассистент"]
    assert _exports_logged(rid) == []
