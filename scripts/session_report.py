"""
Разбор сессии после работы (2.7, мониторинг): всё по одному акту в одном отчёте — чтобы
проверить прогон, не собирая картину по файлам вручную.

Разделы: сессия (акт, тип, статус, стоимость, закрыта ли); узлы (время, стоимость, сбои);
агенты (попытки, исход, время, стоимость, ходы, инструменты, ошибки инструментов по видам);
Гарант и интернет итого; вердикты (с --compare — «было → стало» против другого прогона того
же акта); результат (карта, бланк по видам, обязательные, вопросы органу, Excel); чат, версии,
действия пользователя; проблемы списком.

Работает и с прогонами до 2.7 (без журнала): узлы — из progress.json, агенты — из сырых ответов.

Запуск из корня репозитория:
    .venv/bin/python scripts/session_report.py <run_id> [--compare <run_id или путь>]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import trace  # noqa: E402
from app.chat_common import read_chat_file  # noqa: E402
from app.formatting import format_cost
from app.sessions import split_costs  # noqa: E402
from scripts._monitor import SCHEMA_RETRY, STRUCTURED_OUTPUT, load_raw, tool_calls  # noqa: E402

_RAW = re.compile(r"\.raw(?:\.attempt(\d+))?\.json$")


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _journal(run_path: Path) -> list[dict]:
    path = run_path / trace.EVENTS_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _lines(path: Path) -> list[str]:
    """Непустые строки файла; нет файла — пусто."""
    try:
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def _resolve(run: str) -> Path:
    return Path(run) if Path(run).is_dir() else trace.run_dir(run)


def agents(run_path: Path, journal: list[dict]) -> list[dict]:
    """Агенты сессии по сырым ответам (все попытки) и журналу: исход, стоимость, ходы, инструменты, ошибки."""
    groups: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    for raw in run_path.rglob("*.raw*.json"):
        rel = raw.relative_to(run_path).as_posix()
        m = _RAW.search(rel)
        if m is None or rel.startswith("versions/"):
            continue
        groups[rel[: m.start()]].append((int(m.group(1) or 1), raw))
    outcomes = defaultdict(list)
    for e in journal:
        if e["event"] == "agent_attempt":
            outcomes[e["trace"].removesuffix(".raw.json")].append(e)
    result = []
    for base in sorted(groups):
        files = [p for _, p in sorted(groups[base])]
        cost = turns = seconds = 0.0
        calls = []
        cost_known = True
        cumulative_usage = None
        for path in files:
            events = load_raw(path)
            calls += tool_calls(events)
            codex_finished = next((e for e in reversed(events) if isinstance(e, dict) and e.get("type") == "turn.completed"), None)
            if any(isinstance(e, dict) and e.get("type") == "thread.started" for e in events):
                cost_known = False
            if codex_finished is not None:
                cumulative_usage = codex_finished.get("usage")
            final = next((e for e in events if isinstance(e, dict) and e.get("type") == "result"), None)
            if final:
                cost += float(final.get("total_cost_usd") or 0)
                turns += final.get("num_turns") or 0
                seconds += (final.get("duration_ms") or 0) / 1000
        attempts = outcomes.get(base) or []
        cost_known = cost_known and all(a.get("cost_known", True) for a in attempts)
        done = next((e for e in reversed(journal) if e.get("event") == "agent_finished" and e.get("trace", "").removesuffix(".raw.json") == base), {})
        cost_known = cost_known and done.get("cost_known", True)
        if attempts:
            seconds = sum(a.get("duration_s") or 0 for a in attempts)
        if attempts and all("cost_usd" in a for a in attempts):
            # 15.10 (код-ревью): при --resume CLI сообщает стоимость сессии нарастающим итогом — сумма сырых ответов
            # её завышает; в журнале попыток стоимость уже за попытку (runner.call)
            cost = sum(float(a["cost_usd"] or 0) for a in attempts)
        result.append({
            "agent": base, "attempts": len(files),
            "outcomes": [a["outcome"] for a in attempts],
            "cost_usd": round(cost, 4), "cost_known": cost_known,
            "token_usage": done.get("token_usage"), "session_token_usage": cumulative_usage, "turns": int(turns), "seconds": round(seconds),
            # Запрещённый изоляцией вызов не выполнялся — в счёт инструментов не идёт (виден в ошибках)
            "tools": Counter(c["name"] for c in calls
                             if c["name"] != STRUCTURED_OUTPUT and c["error"] != "не разрешён агенту"),
            "errors": Counter(c["error"] for c in calls if c["error"]),
        })
    return result


def _verdicts(run_path: Path) -> dict[int, dict]:
    out = {}
    for f in sorted((run_path / "03_verdicts").glob("[0-9][0-9].json")):
        data = _json(f)
        if data:
            out[int(f.name[:2])] = data
    return out


def report(run: str, compare: str | None = None) -> str:
    run_path = _resolve(run)
    journal = _journal(run_path)
    meta = _json(run_path / "meta.json") or {}
    progress = _json(run_path / "progress.json") or {}
    summary = _json(run_path / trace.SESSION_SUMMARY_FILE)
    lines: list[str] = []
    problems: list[str] = []
    add = lines.append

    # --- сессия
    add(f"== Сессия {run_path.name}")
    add(f"акт: {meta.get('act_filename')} · тип: {', '.join(meta.get('act_types') or []) or '—'}")
    chat = read_chat_file(run_path / "dialog" / "chat.jsonl")
    # 15.10: та же разбивка, что в итоге сессии (progress после правок — уже итог вместе с диалогом)
    costs = split_costs(journal, meta.get("cost_usd"), progress.get("total_cost_usd"), chat,
                        meta_cost_known=meta.get("cost_known", True))
    add(f"начат: {meta.get('started_at')} · разбор: {progress.get('status')} · "
        f"стоимость: разбор {format_cost(costs['разбор'])} · диалог {format_cost(costs['диалог'])} · всего {format_cost(costs['всего'])}")
    # 23.10: прогон можно открыть заново — итоги прошлых сессий в sessions/NN.json
    for past in sorted((run_path / trace.SESSIONS_DIR).glob("[0-9][0-9].json")):
        old = _json(past) or {}
        add(f"сессия {old.get('session_no', past.stem)}: {old.get('started_at')} — {old.get('closed_at')} "
            f"({old.get('closed_by')}) · всего {format_cost(old['cost_usd']['всего'])} · версия {old.get('version')}")
    if summary:
        add(f"закрыта: {summary.get('closed_at')} ({summary.get('closed_by')}) · "
            f"всего {format_cost(summary['cost_usd']['всего'])} · версия {summary.get('version')}")
    else:
        add("сессия открыта" if journal else "журнала нет (прогон до 2.7)")

    # --- узлы
    add("")
    add("== Узлы")
    node_events = {e["node"]: e for e in journal if e["event"] in ("node_finished", "node_failed")}
    for node in progress.get("nodes") or []:
        e = node_events.get(node["key"], {})
        status = node.get("status")
        add(f"{node['key']:<11} {status:<8} {node.get('duration_s') or 0:>7.1f} с  {format_cost(node.get('cost_usd'), node.get('cost_known', meta.get('cost_known', True)))}"
            + (f"  ⚠ {node.get('error') or e.get('error')}" if status == "сбой" else ""))
        if status == "сбой":
            problems.append(f"узел {node['key']}: {node.get('error') or e.get('error')}")

    # --- агенты
    add("")
    add("== Агенты")
    all_tools: Counter = Counter()
    all_errors: Counter = Counter()
    for a in agents(run_path, journal):
        all_tools += a["tools"]
        all_errors += a["errors"]
        outcome = ",".join(a["outcomes"]) if a["outcomes"] else "—"
        tools = ", ".join(f"{k}×{v}" for k, v in a["tools"].most_common())
        errors = ", ".join(f"{k}×{v}" for k, v in a["errors"].most_common())
        add(f"{a['agent']:<26} попыток {a['attempts']} · {outcome} · {a['seconds']} с · {format_cost(a['cost_usd'], a['cost_known'])} · "
            f"ходов {a['turns']}" + (f" · {tools}" if tools else "") + (f" · ⚠ {errors}" if errors else ""))
        usage = a["token_usage"] or a["session_token_usage"]
        if usage is not None:
            note = "за вызов" if a["token_usage"] is not None else "накопленно по сессии"
            add(f"  токены ({note}): вход {usage.get('input_tokens', '—')}, выход {usage.get('output_tokens', '—')}")
        if a["outcomes"] and a["outcomes"][-1] != "ok":
            problems.append(f"агент {a['agent']}: не удался ({outcome})")
        elif a["outcomes"].count("timeout"):
            problems.append(f"агент {a['agent']}: обрывов по таймауту {a['outcomes'].count('timeout')}")
        # Не проблемы: запрет инструмента — изоляция работает; ответ не по схеме — модель исправилась сама
        real_errors = {k: v for k, v in a["errors"].items() if k not in ("не разрешён агенту", SCHEMA_RETRY)}
        if real_errors:
            problems.append(f"агент {a['agent']}: ошибки инструментов {real_errors}")

    # --- инструменты итого
    add("")
    add("== Гарант и интернет итого")
    garant = {k: v for k, v in all_tools.items() if k.startswith("garant_")}
    web = {k: v for k, v in all_tools.items() if k in ("WebSearch", "WebFetch")}
    exports = sum(v for k, v in garant.items() if k.startswith("garant_export"))
    add(f"Гарант: {sum(garant.values())} вызовов {garant or ''} · выгрузок {exports}")
    add(f"интернет: {web or 'нет'}")
    add(f"ошибки инструментов: {dict(all_errors) or 'нет'}")

    # --- вердикты
    add("")
    add("== Вердикты" + (f" (было → стало против {Path(compare).name})" if compare else ""))
    now = _verdicts(run_path)
    before = _verdicts(_resolve(compare)) if compare else {}
    for n in range(1, 13):
        v = now.get(n)
        brief = " ".join((v or {}).get("rationale_brief") or [])[:160]
        if compare:
            old = (before.get(n) or {}).get("verdict")
            new = (v or {}).get("verdict")
            mark = "  *" if old != new else ""
            add(f"{n:>2}: {old} → {new}{mark} | {brief}")
        else:
            add(f"{n:>2}: {(v or {}).get('verdict')} | {brief}")
        if v is None or v.get("verdict") == "ошибка":
            problems.append(f"индикатор {n}: {'нет файла вердикта' if v is None else 'ошибка'}")

    # --- результат
    add("")
    add("== Результат")
    rows = (_json(run_path / "06_map.json") or {}).get("rows") or []
    items = (_json(run_path / "07_request.json") or {}).get("items") or []
    kinds = Counter(i.get("kind") for i in items)
    add(f"карта: {len(rows)} строк · бланк: {len(items)} позиций {dict(kinds)} · "
        f"обязательных {sum(1 for i in items if i.get('mandatory'))} · Excel: "
        f"{'есть' if (run_path / '08_result.xlsx').is_file() else 'НЕТ'}")
    passport = _json(run_path / "02_passport.json") or {}
    developers = [d.get("name") for d in (passport.get("requisites") or {}).get("developers") or []]
    add(f"разработчик: {'; '.join(developers) or '—'}")
    if not (run_path / "08_result.xlsx").is_file():
        problems.append("Excel не собран")

    # --- чат, версии, действия
    if journal:
        add("")
        add("== Чат, версии, действия пользователя")
        chat = Counter(e.get("role") for e in journal if e["event"] == "chat")
        versions = [e.get("version") for e in journal if e["event"] == "version_created"]
        actions = [e.get("action") for e in journal if e["event"] == "user_action"]
        add(f"реплик: {dict(chat) or 'нет'} · версии: {versions or 'нет'} · действий: {len(actions)}")
        for e in journal:
            if e["event"] == "user_action":
                add(f"  {e['ts'][:19]} {e.get('action')}")

    add("")
    add("== Проблемы")
    lines.extend(f"- {p}" for p in problems) if problems else add("нет")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Разбор сессии П-10")
    parser.add_argument("run", help="id прогона (папка в runs/) или путь к папке")
    parser.add_argument("--compare", help="прогон того же акта для «было → стало» по вердиктам")
    args = parser.parse_args(argv)
    if not _resolve(args.run).is_dir():
        print(f"нет папки прогона: {args.run}", file=sys.stderr)
        return 2
    print(report(args.run, args.compare))
    return 0


if __name__ == "__main__":
    sys.exit(main())
