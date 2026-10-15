"""
Тесты инструментов мониторинга (2.7): живая лента scripts/watch.py и разбор scripts/session_report.py.
Папка прогона собирается вручную: журнал, файл хода агента, сырые ответы с вызовами Гаранта, вердикты.
"""

from __future__ import annotations

import json

from app.trace import log_event, new_run, run_dir, save_json
from scripts import session_report, watch


def _agent_events(ok: bool = True) -> list[dict]:
    """События агента индикатора 3: вызов Гаранта с обрывом, вызов с ответом, запрещённый инструмент, итог."""
    def use(i, name, inp):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": name,
                                                              "input": inp}]}}

    def res(i, text, err=False):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                         "content": [{"type": "text", "text": text}],
                                                         "is_error": err}]}}
    events = [
        {"type": "system", "subtype": "init", "mcp_servers": [{"name": "garant", "status": "connected"}]},
        use(1, "mcp__garant__garant_search", {"query": "152-ПП"}),
        res(1, 'MCP server "garant" transport dropped mid-call; response for tool "garant_search" was lost', True),
        use(2, "mcp__garant__garant_document_redactions", {"topic": 70166342}),
        res(2, "[{\"activity\": []}]"),
        use(3, "mcp__garant__garant_export_html", {"topic": 1}),
        res(3, "Permission to use mcp__garant__garant_export_html has been denied because ...", True),
        use(4, "StructuredOutput", {}),
        res(4, "Output does not match required schema: /x", True),
        use(5, "WebFetch", {"url": "https://mos.ru"}),
        res(5, "timeout of 60000ms exceeded", True),
        use(6, "mcp__garant__garant_entry_text", {"topic": 1}),
        res(6, "<tool_use_error>Error: No such tool available: mcp__garant__garant_entry_text</tool_use_error>", True),
    ]
    if ok:
        events.append({"type": "result", "subtype": "success", "total_cost_usd": 0.37, "num_turns": 20,
                       "duration_ms": 760000})
    return events


def _run(tmp_runs_dir) -> str:
    rid = new_run("акт.docx", act_types=["господдержка"])
    log_event(rid, "node_started", who="узел verdicts", node="verdicts", inputs=["01_document.md"])
    raw = run_dir(rid) / "03_verdicts" / "03.raw.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(_agent_events()), encoding="utf-8")
    with open(run_dir(rid) / "03_verdicts" / "03.stream.jsonl", "w", encoding="utf-8") as f:
        for i, e in enumerate(_agent_events()):
            f.write(json.dumps({"ts": f"2026-10-13T10:00:{i:02d}.000", "attempt": 1, "event": e}) + "\n")
    log_event(rid, "agent_attempt", trace="03_verdicts/03.raw.json", attempt=1, max_attempts=3, outcome="ok",
              duration_s=760.0)
    for n in range(1, 13):
        save_json(rid, f"03_verdicts/{n:02d}.json", {"indicator": n, "verdict": "нарушен" if n == 3 else "соблюдён",
                                                      "rationale_brief": [f"выжимка {n}"]})
    save_json(rid, "progress.json", {"run_id": rid, "status": "готов", "total_cost_usd": 4.8, "nodes": [
        {"key": "verdicts", "title": "Индикаторы", "status": "готов", "duration_s": 560.0, "cost_usd": 1.4},
        {"key": "sort", "title": "Разбор", "status": "сбой", "duration_s": 0.1, "cost_usd": 0, "error": "сломалось"}]})
    save_json(rid, "07_request.json", {"items": [{"kind": "показатель по годам", "mandatory": ["ГП-1"]},
                                                 {"kind": "открытый вопрос", "mandatory": []}]})
    return rid


def test_report_shows_agents_tools_and_problems(tmp_runs_dir):
    rid = _run(tmp_runs_dir)
    text = session_report.report(rid)
    line = next(ln for ln in text.splitlines() if ln.startswith("03_verdicts/03"))
    assert "попыток 1 · ok · 760 с · $0.37 · ходов 20" in line
    assert "garant_search×1" in line and "garant_document_redactions×1" in line and "StructuredOutput" not in line.split("⚠")[0]
    assert "garant_export_html" not in line.split("⚠")[0]   # запрещённый вызов не выполнялся — не в счёт инструментов
    assert "обрыв связи×1" in line and "не разрешён агенту×1" in line
    problems = text.split("== Проблемы")[1]
    assert ("агент 03_verdicts/03: ошибки инструментов {'обрыв связи': 1, 'таймаут': 1, 'нет такого инструмента': 1}"
            in problems)   # запрет изоляцией и ответ не по схеме — не проблемы
    assert "узел sort: сломалось" in problems
    assert "Excel не собран" in problems
    assert "обязательных 1" in text and "'открытый вопрос': 1" in text
    assert " 3: нарушен | выжимка 3" in text


def test_report_compare_marks_changed_verdicts(tmp_runs_dir):
    old = _run(tmp_runs_dir)
    new = _run(tmp_runs_dir)
    save_json(new, "03_verdicts/05.json", {"indicator": 5, "verdict": "нет данных", "rationale_brief": ["нет"]})
    text = session_report.report(new, compare=old)
    assert " 5: соблюдён → нет данных  * | нет" in text and " 4: соблюдён → соблюдён | выжимка 4" in text


def test_report_on_run_without_journal(tmp_runs_dir):
    """Прогон до 2.7 (журнала нет) — отчёт строится по progress.json и сырым ответам."""
    rid = _run(tmp_runs_dir)
    (run_dir(rid) / "events.jsonl").unlink()
    text = session_report.report(rid)
    assert "журнала нет" in text and "03_verdicts/03" in text


def test_report_agent_cost_from_journal_not_cumulative(tmp_runs_dir):
    """15.10 (код-ревью): при --resume CLI сообщает стоимость всей сессии нарастающим итогом — стоимость агента
    в отчёте берётся из журнала попыток (там она уже за попытку), а не суммой сырых ответов."""
    rid = new_run("акт.docx")
    for name, total in (("0001.raw.json", 0.5), ("0001.raw.attempt2.json", 0.8)):
        path = run_dir(rid) / "dialog" / "turns" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([{"type": "result", "total_cost_usd": total, "num_turns": 2,
                                     "duration_ms": 1000}]), encoding="utf-8")
    log_event(rid, "agent_attempt", trace="dialog/turns/0001.raw.json", attempt=1, max_attempts=3,
              outcome="validation_error", duration_s=1.0, cost_usd=0.5)
    log_event(rid, "agent_attempt", trace="dialog/turns/0001.raw.json", attempt=2, max_attempts=3,
              outcome="ok", duration_s=1.0, cost_usd=0.3)
    line = next(ln for ln in session_report.report(rid).splitlines() if ln.startswith("dialog/turns/0001"))
    assert "$0.80" in line


def test_report_cost_line_splits_run_and_dialog(tmp_runs_dir):
    """15.10 (повторное ревью): после правок progress хранит итог вместе с диалогом — строка стоимости отчёта делит
    разбор и диалог так же, как итог сессии (одна функция sessions.run_costs)."""
    from app.contracts import RunMeta
    from app.dialog import append_chat
    rid = new_run("акт.docx")
    save_json(rid, "progress.json", {"run_id": rid, "status": "готов", "total_cost_usd": 13.0, "nodes": []})
    save_json(rid, "meta.json", RunMeta(run_id=rid, act_filename="акт.docx", started_at="2026-10-13T10:00:00",
                                        finished_at="2026-10-13T10:08:00", status="finished", cost_usd=13.0))
    log_event(rid, "run_finished", status="готов", cost_usd=10.0)
    append_chat(rid, "ассистент", "ответ", cost_usd=3.0)
    assert "стоимость: разбор $10.00 · диалог $3.00 · всего $13.00" in session_report.report(rid)


def test_watch_merges_journal_and_agent_stream(tmp_runs_dir, capsys):
    rid = _run(tmp_runs_dir)
    assert watch.main([rid]) == 0
    out = capsys.readouterr().out
    assert "узел verdicts" in out and "старт (вход: 01_document.md)" in out
    assert "агент 03_verdicts/03" in out and "→ garant_search query=152-ПП" in out
    assert "⚠ ошибка инструмента (обрыв связи)" in out
    assert "итог: success · $0.37 · ходов 20 · 760 с" in out
    assert "⚠ ошибка инструмента (ответ не прошёл проверку CLI (модель исправила))" in out


def test_watch_reads_only_new_complete_lines(tmp_runs_dir):
    """Слежение: второе чтение отдаёт только новые строки; недописанная строка ждёт конца."""
    rid = _run(tmp_runs_dir)
    offsets = {}
    first, _ = watch.collect(run_dir(rid), offsets)
    assert first
    stream = run_dir(rid) / "03_verdicts" / "03.stream.jsonl"
    with open(stream, "a", encoding="utf-8") as f:
        f.write('{"ts": "2026-10-13T10:01:00.000", "attempt": 2, "text": "строка"}\n{"ts": "2026-10-13T10:0')
    second, _ = watch.collect(run_dir(rid), offsets)
    assert [r[2] for r in second] == ["[п.2] вывод: строка"]
