"""The ``weekly`` commands: ``start``, ``rows``, ``find``, ``mark``, ``report``, ``brief``.
All of them only read the original."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import weekly
from .send import run_gh

COMMANDS = ("weekly",)


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("weekly", help="check the backlog against the original's work")
    steps = parser.add_subparsers(dest="step", required=True)
    start = steps.add_parser("start", help="pull the original's lists for a backlog")
    start.add_argument("--backlog", required=True, type=Path, help="a file, one row per line")
    steps.add_parser("rows", help="the backlog rows and their results")
    find = steps.add_parser("find", help="entries whose title holds every word")
    find.add_argument("words", nargs="+")
    mark = steps.add_parser("mark", help="record one row's result")
    mark.add_argument("row", type=int)
    mark.add_argument("result", choices=weekly.KINDS)
    mark.add_argument("--why", required=True, help="one line: what decided it")
    mark.add_argument("--link", default="", help="the pull request, issue or RFC on the original")
    steps.add_parser("report", help="write the page for this check")
    steps.add_parser("brief", help="print the worker's brief")


def run_command(args: argparse.Namespace) -> int:
    try:
        if args.step == "brief":
            print(weekly.worker_brief())
            return 0
        if args.step == "start":
            rows = weekly.backlog_rows(args.backlog.read_text(encoding="utf-8"))
            date = weekly.start(rows, run_gh)
            print(f"weekly check {date}: {len(rows)} rows. {weekly.counts(date)}.")
            return 0
        date = weekly.current()
        if args.step == "find":
            print("\n".join(weekly.found_lines(weekly.find(date, args.words))))
        elif args.step == "mark":
            weekly.mark(date, args.row, args.result, args.why, args.link)
            print(f"row {args.row}: {weekly.WORDS[args.result]}")
        elif args.step == "report":
            path, left = weekly.report(date)
            print(f"{path}\n{left} rows not checked")
            return 1 if left else 0
        else:
            print("\n".join(weekly.row_lines(date)))
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from error
    return 0
