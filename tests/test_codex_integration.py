"""Инструменты, происхождение сессий и новые события; без вызовов моделей и правовой базы."""
import json
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest

from app import chat_common, config, dialog, garant_chat, runner, trace, versions
from app.contracts import ChatEntry, CliSession, DialogTurn, GarantTurn, RunMeta
from app.runner import CallResult
from app.sessions import split_costs
from scripts import _monitor
from scripts.session_report import report
from test_codex_runner import codex, prepared, invoke, THREAD, USAGE, ANSWER
from test_dialog import rid
from test_garant_chat import chat


def options(command):
    values = {}
    for i, arg in enumerate(command):
        if arg == "-c":
            key = command[i+1].split("=",1)[0]
            value = tomllib.loads(command[i+1])
            for part in key.split("."):
                value = value[part]
            values[key] = value
    return values


def item(tool="garant_search", server="garant", id="m1", status="completed"):
    event = {"type":"item.completed","item":{"id":id,"type":"mcp_tool_call",
            "server":server,"tool":tool,"arguments":{"query":"номер акта"},
            "status":status,"result":{"content":[{"type":"text","text":"найден акт"}]}}}
    if status == "failed":
        event["item"].update(result=None, error={"message":"timeout"})
    return event


def result(value, backend="codex", usage=None):
    return CallResult(value=value,ok=True,attempts=1,cost_usd=0.0 if backend=="codex" else .1,
                      session_id=THREAD,error=None,permission_denials=[],duration_s=1,
                      backend=backend,model=config.BACKEND_MODELS[backend],
                      cost_known=backend=="claude",token_usage=usage or USAGE,
                      session_token_usage=usage or USAGE)


def test_codex_garant_exposes_only_requested_tools(codex,tmp_runs_dir,monkeypatch):
    calls=prepared(monkeypatch,[{}])
    assert invoke(mcp_tools=["garant_search","garant_document_redactions"]).ok
    cmd,args=calls[0];opts=options(cmd)
    assert opts["mcp_servers.garant.enabled_tools"]==["garant_search","garant_document_redactions"]
    assert opts["mcp_servers.garant.required"] is True
    assert opts["mcp_servers.garant.bearer_token_env_var"]=="GARANT_TOKEN"
    assert opts["mcp_servers.garant.tools.garant_search.output_token_limit"]==100000
    assert args["env"]["GARANT_TOKEN"]=="TEST-TOKEN"
    assert "TEST-TOKEN" not in json.dumps(cmd)
    assert opts["web_search"]=="disabled" and opts["features.shell_tool"] is False


def test_p10_reader_is_bound_to_current_run_without_garant(codex,tmp_runs_dir,monkeypatch,tmp_path):
    cfg=Path(dialog.mcp_config("probe",with_garant=False))
    calls=prepared(monkeypatch,[{}])
    assert invoke(mcp_tools=["mcp__p10run__read_artifact"],mcp_config=str(cfg)).ok
    opts=options(calls[0][0])
    assert opts["mcp_servers.p10run.enabled_tools"]==["read_artifact"]
    assert opts["mcp_servers.p10run.env.P10_RUN_DIR"]==str(trace.run_dir("probe").resolve())
    assert not any("mcp_servers.garant" in x for x in opts)
    assert "GARANT_TOKEN" not in calls[0][1]["env"]


def test_search_is_enabled_only_for_web_nodes(codex,tmp_runs_dir,monkeypatch):
    calls=prepared(monkeypatch,[{}])
    args=dict(instruction="проверь",document="акт",model_cls=__import__("test_codex_runner").Probe,
              run_id="probe",trace_name="probe.raw.json",allowed_tools=["WebSearch","WebFetch"])
    assert runner.call(**args).ok
    assert options(calls[0][0])["web_search"]=="live"


def test_allowed_mcp_event_is_accepted(codex,tmp_runs_dir,monkeypatch):
    events=[{"type":"thread.started","thread_id":THREAD},item(),
            {"type":"turn.completed","usage":USAGE}]
    prepared(monkeypatch,[{"events":events}])
    assert invoke(mcp_tools=["garant_search"]).ok


def test_unrequested_mcp_event_is_rejected(codex,tmp_runs_dir,monkeypatch):
    events=[{"type":"thread.started","thread_id":THREAD},item(tool="garant_export_html"),
            {"type":"turn.completed","usage":USAGE}]
    prepared(monkeypatch,[{"events":events}])
    out=invoke(mcp_tools=["garant_search"],max_attempts=1)
    assert not out.ok and "unexpected_tool" in out.error


def test_failed_mcp_result_can_be_recovered_by_model(codex,tmp_runs_dir,monkeypatch):
    events=[{"type":"thread.started","thread_id":THREAD},item(status="failed"),
            {"type":"turn.completed","usage":USAGE}]
    prepared(monkeypatch,[{"events":events}])
    assert invoke(mcp_tools=["garant_search"]).ok  # ошибка инструмента остаётся в трейсах


def test_inline_mcp_secret_is_not_written_to_command(codex,tmp_runs_dir,monkeypatch,tmp_path):
    cfg=tmp_path/"mcp.json"
    cfg.write_text(json.dumps({"mcpServers":{"garant":{"type":"http","url":"https://mcp.garant.ru/mcp",
                   "headers":{"Authorization":"Bearer DO-NOT-EXPOSE"}}}}))
    calls=prepared(monkeypatch,[])
    with pytest.raises(ValueError):
        invoke(mcp_tools=["garant_search"],mcp_config=str(cfg))
    assert calls==[]


@pytest.mark.parametrize("target,other",[("codex","claude"),("claude","codex")])
def test_result_chat_switches_backend_preserving_history(rid,monkeypatch,target,other):
    monkeypatch.setattr(config,"LLM_BACKEND",target)
    monkeypatch.setattr(dialog,"DIALOG_MODEL",config.BACKEND_MODELS[target])
    dialog.append_chat(rid,"пользователь","СТАРЫЙ ВОПРОС")
    dialog.append_chat(rid,"ассистент","СТАРЫЙ ОТВЕТ")
    old=CliSession(session_id=THREAD,started="2026-10-01",backend=other,token_reported=USAGE)
    trace.save_json(rid,dialog.SESSION,old)
    mock=Mock(return_value=result(DialogTurn(reply="новый ответ"),target))
    monkeypatch.setattr(dialog,"call",mock)
    dialog.turn(rid,"новый вопрос")
    kw=mock.call_args.kwargs
    assert "resume_session_id" not in kw
    assert "СТАРЫЙ ВОПРОС" in mock.call_args.args[1]
    assert dialog.load_session(rid).backend==target
    assert list((trace.run_dir(rid)/"dialog/cli_sessions").glob("*.json"))
    assert len(dialog.read_chat(rid))==4


def test_result_chat_codex_resume_passes_previous_usage(rid,monkeypatch):
    monkeypatch.setattr(config,"LLM_BACKEND","codex")
    monkeypatch.setattr(dialog,"DIALOG_MODEL","gpt-6.1-terra")
    mock=Mock(return_value=result(DialogTurn(reply="ответ")))
    monkeypatch.setattr(dialog,"call",mock)
    dialog.turn(rid,"первый");dialog.turn(rid,"второй")
    assert mock.call_args.kwargs["resume_backend"]=="codex"
    assert mock.call_args.kwargs["resume_token_base"]==USAGE
    last=dialog.read_chat(rid)[-1]
    assert last.cost_usd is None and last.cost_known is False
    assert last.backend=="codex"


@pytest.mark.parametrize("target,other",[("codex","claude"),("claude","codex")])
def test_garant_chat_switches_backend_without_losing_documents(chat,monkeypatch,target,other):
    monkeypatch.setattr(config,"LLM_BACKEND",target)
    monkeypatch.setattr(garant_chat,"DIALOG_MODEL",config.BACKEND_MODELS[target])
    root=trace.run_dir(garant_chat.chat_run_id(chat))
    from app.contracts import GarantDocument
    doc=GarantDocument(n=1,name="акт.docx",chars=8,at="2026-10-30")
    info=garant_chat.chat_info(chat).model_copy(update={"documents":[doc]})
    (root/garant_chat.CHAT_INFO).write_text(info.model_dump_json())
    (root/"docs").mkdir();(root/"docs/01.md").write_text("ДОКУМЕНТ")
    (root/garant_chat.SESSION).write_text(CliSession(session_id=THREAD,started="2026-10-01",
                                           backend=other,token_reported=USAGE).model_dump_json())
    garant_chat.append_chat(chat,"пользователь","история")
    mock=Mock(return_value=result(GarantTurn(reply="ответ"),target));monkeypatch.setattr(garant_chat,"call",mock)
    garant_chat.turn(chat,"вопрос")
    assert "resume_session_id" not in mock.call_args.kwargs
    assert "история" in mock.call_args.args[1]
    assert "ДОКУМЕНТ" in mock.call_args.kwargs["shared_context"]
    assert garant_chat.chat_info(chat).documents==[doc]
    assert (root/"docs/01.md").read_text()=="ДОКУМЕНТ"
    assert list((root/"cli_sessions").glob("*.json"))
    assert json.loads((root/garant_chat.SESSION).read_text())["backend"]==target


def test_garant_chat_codex_resume_and_unknown_price(chat,monkeypatch):
    monkeypatch.setattr(config,"LLM_BACKEND","codex")
    monkeypatch.setattr(garant_chat,"DIALOG_MODEL","gpt-6.1-terra")
    mock=Mock(return_value=result(GarantTurn(reply="ответ")));monkeypatch.setattr(garant_chat,"call",mock)
    garant_chat.turn(chat,"один");garant_chat.turn(chat,"два")
    assert mock.call_args.kwargs["resume_backend"]=="codex"
    assert mock.call_args.kwargs["resume_token_base"]==USAGE
    assert garant_chat.read_chat(chat)[-1].cost_usd is None
    assert garant_chat.list_chats()[0]["cost_usd"] is None


def test_codex_steps_have_one_entry_per_call_and_keep_errors(tmp_path):
    events=[{"type":"item.started","item":{**item()["item"],"status":"in_progress"}},
            item(),item(tool="garant_export_html",id="m2",status="failed"),
            {"type":"item.completed","item":{"id":"web1","type":"web_search","query":"акт","action":{"type":"search","query":"акт"}}}]
    (tmp_path/"0001.stream.jsonl").write_text("".join(json.dumps({"event":e})+"\n" for e in events))
    uses,errors=chat_common.turn_tools(tmp_path,1)
    assert len(uses)==3 and errors=={"m2"}
    assert uses[0]["name"]=="mcp__garant__garant_search"
    assert "ищу в интернете" in chat_common.tool_step_text(uses[-1]["name"],uses[-1]["input"])
    calls=_monitor.tool_calls(events)
    assert len(calls)==3 and calls[0]["name"]=="garant_search"


def test_unknown_price_is_not_reported_as_free():
    chat=[ChatEntry(at="2026-10-30",role="ассистент",text="ответ",cost_known=False,backend="codex")]
    costs=split_costs([{"event":"run_finished","cost_usd":2.0}],2.0,2.0,chat,meta_cost_known=False)
    assert costs=={"разбор":2.0,"диалог":None,"всего":None}
    text=" ".join(_monitor.describe_agent_event({"type":"turn.completed","usage":USAGE}))
    assert "5927" in text and "$0.00" not in text


def test_report_with_codex_does_not_invent_usd(tmp_runs_dir):
    rid=trace.new_run("акт")
    meta=trace.load_json(rid,trace.META_FILE,RunMeta).model_copy(update={"cost_known":False,"llm_backend":"codex"})
    trace.save_json(rid,trace.META_FILE,meta)
    text=report(str(trace.run_dir(rid)))
    assert "не определена" in text


def test_incomplete_codex_export_is_not_counted_as_spent(rid):
    turns=trace.run_dir(rid)/"dialog/turns";turns.mkdir(parents=True)
    event=item(tool="garant_export_html",status="in_progress")
    event["type"]="item.started"
    (turns/"0001.stream.jsonl").write_text(json.dumps({"event":event})+"\n")
    assert dialog._turn_exports(rid,1)==0


def test_unknown_price_survives_candidate_commit_and_excel(rid):
    from app import operations
    from app.contracts import RecheckIndicator, Verdict
    from openpyxl import load_workbook
    verdict=trace.load_json(rid,f"{trace.VERDICTS_DIR}/01.json",Verdict)
    op=RecheckIndicator(kind="перепроверить индикатор",indicator=1,clarification="проверь",reason="тест")
    candidate=operations.Candidate(op,verdict,"до","после",0.0,cost_known=False)
    out=operations.accept(rid,candidate)
    assert out.cost_known is False and versions.read_journal(rid)[-1].cost_known is False
    wb=load_workbook(trace.run_dir(rid)/trace.EXCEL_FILE)
    assert wb["Журнал изменений"].cell(3,9).value=="не определена"


def test_codex_progress_and_result_events_mark_unknown_cost(codex,tmp_runs_dir):
    from app.pipeline import _Progress
    from app.contracts import RunSummary
    rid=trace.new_run("акт");progress=_Progress(rid,"акт")
    progress.node_started("convert");progress.node_finished("convert")
    progress.finish("готов",RunSummary())
    saved=trace.read_progress(rid)
    assert saved.cost_known is False and saved.nodes[0].cost_known is False
    assert trace.read_events(rid)[-1]["cost_known"] is False


def test_missing_codex_counter_starts_new_result_session_with_history(rid,monkeypatch):
    monkeypatch.setattr(config,"LLM_BACKEND","codex")
    monkeypatch.setattr(dialog,"DIALOG_MODEL","gpt-6.1-terra")
    trace.save_json(rid,dialog.SESSION,CliSession(session_id=THREAD,started="2026-10-30",backend="codex"))
    dialog.append_chat(rid,"пользователь","история")
    mock=Mock(return_value=result(DialogTurn(reply="ответ")));monkeypatch.setattr(dialog,"call",mock)
    dialog.turn(rid,"дальше")
    assert "resume_session_id" not in mock.call_args.kwargs
    assert "история" in mock.call_args.args[1]
    assert list((trace.run_dir(rid)/"dialog/cli_sessions").glob("*.json"))


def test_missing_codex_counter_starts_new_garant_session(chat,monkeypatch):
    monkeypatch.setattr(config,"LLM_BACKEND","codex")
    monkeypatch.setattr(garant_chat,"DIALOG_MODEL","gpt-6.1-terra")
    root=trace.run_dir(garant_chat.chat_run_id(chat))
    (root/garant_chat.SESSION).write_text(CliSession(session_id=THREAD,started="2026-10-30",backend="codex").model_dump_json())
    mock=Mock(return_value=result(GarantTurn(reply="ответ")));monkeypatch.setattr(garant_chat,"call",mock)
    garant_chat.turn(chat,"дальше")
    assert "resume_session_id" not in mock.call_args.kwargs
    assert list((root/"cli_sessions").glob("*.json"))


def test_recorded_cli_notice_is_displayed_without_becoming_tool_error():
    path=Path(__file__).parent/"fixtures/codex_first.events.jsonl"
    events=[json.loads(line) for line in path.read_text().splitlines()]
    notice=next(e for e in events if e.get("item",{}).get("type")=="error")
    assert _monitor.describe_agent_event(notice)[0].startswith("предупреждение CLI")
    assert _monitor.tool_calls([notice])==[]


def test_codex_human_log_does_not_claim_a_free_call(codex, tmp_runs_dir, monkeypatch):
    prepared(monkeypatch, [{}])
    assert invoke().ok
    log = (trace.run_dir("probe") / "run.log").read_text()
    assert "cost=не определена" in log
    assert "cost=$0.0000" not in log


@pytest.mark.parametrize("event_server", ["codex", "garant"])
@pytest.mark.parametrize("method", ["list_mcp_resources", "list_mcp_resource_templates"])
@pytest.mark.parametrize("arguments", [{}, {"server": "garant"}])
def test_codex_native_discovery_of_configured_mcp_is_accepted(codex, tmp_runs_dir, monkeypatch, method, arguments, event_server):
    discovery = item(tool=method, server=event_server)
    discovery["item"]["arguments"] = arguments
    prepared(monkeypatch, [{"events": [{"type": "thread.started", "thread_id": THREAD}, discovery,
                                      {"type": "turn.completed", "usage": USAGE}]}])
    assert invoke(mcp_tools=["garant_search"], max_attempts=1).ok


@pytest.mark.parametrize("method,arguments,tools", [
    ("list_mcp_resources", {"server": "personal"}, ["garant_search"]),
    ("list_mcp_resources", {}, None),
    ("read_mcp_resource", {}, ["garant_search"]),
    ("list_mcp_resources", {"unknown": "argument"}, ["garant_search"]),
    ("list_mcp_resources", {"server": ["garant"]}, ["garant_search"]),
])
def test_codex_discovery_does_not_allow_other_servers_or_methods(codex, tmp_runs_dir, monkeypatch, method, arguments, tools):
    discovery = item(tool=method, server="codex")
    discovery["item"]["arguments"] = arguments
    prepared(monkeypatch, [{"events": [{"type": "thread.started", "thread_id": THREAD}, discovery,
                                      {"type": "turn.completed", "usage": USAGE}]}])
    out = invoke(mcp_tools=tools, max_attempts=1)
    assert not out.ok and "unexpected_tool" in out.error
