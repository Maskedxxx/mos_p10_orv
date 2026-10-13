"""
Живая лента сессии (2.7, мониторинг): журнал сессии (events.jsonl) и ход каждого агента
(*.stream.jsonl) одной лентой по времени — какой узел идёт, какой агент что вызывает,
где ошибки инструментов и обрывы.

Запуск из корня репозитория:
    .venv/bin/python scripts/watch.py <run_id>              — всё, что есть, и выйти
    .venv/bin/python scripts/watch.py <run_id> -f           — дальше следить за новыми строками
    .venv/bin/python scripts/watch.py <run_id> -f --until-done   — следить до конца разбора
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import trace  # noqa: E402
from scripts._monitor import describe_agent_event, describe_journal_event  # noqa: E402


def _read_new(path: Path, offsets: dict[Path, int]) -> list[str]:
    """Новые полные строки файла с прошлого чтения; недописанная последняя строка ждёт следующего раза."""
    start = offsets.get(path, 0)
    with open(path, "rb") as f:
        f.seek(start)
        chunk = f.read()
    end = chunk.rfind(b"\n") + 1
    offsets[path] = start + end
    return chunk[:end].decode("utf-8", errors="replace").splitlines()


def collect(run_path: Path, offsets: dict[Path, int]) -> tuple[list[tuple[str, str, str]], set[str]]:
    """Новые строки ленты (время, кто, что) из журнала и файлов хода; второе — какие события журнала встретились."""
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    journal = run_path / trace.EVENTS_FILE
    if journal.is_file():
        for line in _read_new(journal, offsets):
            e = json.loads(line)
            seen.add(e["event"])
            text = describe_journal_event(e)
            if text:
                rows.append((e["ts"], e.get("who", ""), text))
    for stream in sorted(run_path.rglob("*.stream.jsonl")):
        who = "агент " + stream.relative_to(run_path).as_posix().removesuffix(".stream.jsonl")
        for line in _read_new(stream, offsets):
            entry = json.loads(line)
            if "event" in entry:
                texts = describe_agent_event(entry["event"])
            else:
                texts = ["вывод: " + entry.get("text", "")[:120]]
            prefix = f"[п.{entry.get('attempt')}] " if entry.get("attempt", 1) != 1 else ""
            rows.extend((entry["ts"], who, prefix + t) for t in texts)
    rows.sort(key=lambda r: r[0])
    return rows, seen


def format_row(row: tuple[str, str, str]) -> str:
    ts, who, text = row
    return f"{ts[11:19]}  {who:<30} {text}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Живая лента сессии П-10")
    parser.add_argument("run", help="id прогона (папка в runs/) или путь к папке")
    parser.add_argument("-f", "--follow", action="store_true", help="следить за новыми строками")
    parser.add_argument("--until-done", action="store_true", help="с -f: выйти, когда разбор закончен или сессия закрыта")
    parser.add_argument("--interval", type=float, default=1.0, help="как часто перечитывать, с")
    args = parser.parse_args(argv)
    run_path = Path(args.run) if Path(args.run).is_dir() else trace.run_dir(args.run)
    if not run_path.is_dir():
        print(f"нет папки прогона: {run_path}", file=sys.stderr)
        return 2
    offsets: dict[Path, int] = {}
    rows, seen = collect(run_path, offsets)
    for row in rows:
        print(format_row(row), flush=True)
    while args.follow:
        if args.until_done and seen & {"run_finished", "session_closed"}:
            break
        time.sleep(args.interval)
        rows, new_seen = collect(run_path, offsets)
        seen |= new_seen
        for row in rows:
            print(format_row(row), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
