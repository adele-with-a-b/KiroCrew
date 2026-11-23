"""Commands: ``pending``, ``verdict``, ``carry``, ``list``, ``brief``, and the ones in
``cli_prepare`` and ``cli_weekly``. Run inside a fork clone."""

from __future__ import annotations

import argparse
import datetime

from . import UPSTREAM, cli_prepare, cli_weekly, ledger
from .changes import Change, Run, git_changes, patch_id, run_git
from .watch import judge_brief, unjudged


def _line(change: Change) -> str:
    number = f" (PR #{change.pr})" if change.pr else ""
    return f"{change.sha[:10]} [{change.branch}] {change.subject}{number}"


def _find(changes: list[Change], prefix: str) -> Change:
    hits = [change for change in changes if change.sha.startswith(prefix.lower())]
    if len(hits) != 1:
        raise SystemExit(f"{prefix}: matches {len(hits)} fork changes; name exactly one")
    return hits[0]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _verdict(args: argparse.Namespace, changes: list[Change], run: Run) -> int:
    change = _find(changes, args.sha)
    row = {
        "sha": change.sha,
        "patch_id": patch_id(change.sha, run),
        "branch": change.branch,
        "pr": args.pr if args.pr is not None else change.pr,
        "subject": change.subject,
        "verdict": args.verdict,
        "why": args.why,
        "strip": args.strip,
        "upstream_match": args.upstream_match,
        "judged_at": _now(),
        "by": args.by,
    }
    try:
        ledger.append(ledger.verdicts_path(), row)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"{args.verdict}  {_line(change)}")
    return 0


def _carry(changes: list[Change], rows: list[dict], run: Run) -> int:
    by_patch = {row["patch_id"]: row for row in rows if row.get("patch_id")}
    carried = 0
    for change in unjudged(changes, rows):
        old = by_patch.get(patch_id(change.sha, run))
        if old is None:
            continue
        row = dict(old, sha=change.sha, branch=change.branch, judged_at=_now())
        row["by"] = f"carry of {old['sha'][:10]}"
        ledger.append(ledger.verdicts_path(), row)
        carried += 1
        print(f"{row['verdict']}  {_line(change)}  (same diff as {old['sha'][:10]})")
    print(f"{carried} carried")
    return 0


def _list(changes: list[Change], rows: list[dict]) -> int:
    judged = ledger.latest(rows)
    for change in changes:
        row = judged.get(change.sha)
        print(f"{row['verdict'] if row else 'UNJUDGED':<17}{_line(change)}")
        if row:
            print(f"{'':<17}{row['why']}")
    return 0


def main(argv: list[str], run: Run = run_git) -> int:
    parser = argparse.ArgumentParser(prog="kc_upstream", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pending", help="list fork changes that have no verdict")
    commands.add_parser("list", help="list every fork change with its verdict")
    commands.add_parser("carry", help="copy verdicts onto changes that only moved commits")
    commands.add_parser("brief", help="print the judge's brief for the pending changes")
    verdict = commands.add_parser("verdict", help="record one change's verdict")
    verdict.add_argument("sha", help="the change's commit id, or a unique start of it")
    verdict.add_argument("verdict", choices=ledger.VERDICTS)
    verdict.add_argument("--why", required=True, help="one line: what decided it")
    verdict.add_argument("--strip", default="none", help="what to remove before sending")
    verdict.add_argument("--upstream-match", default="none", help=f"a link on {UPSTREAM}")
    verdict.add_argument("--pr", type=int, help="the fork pull request, when not a merge commit")
    verdict.add_argument("--by", default="judge", help="who judged")
    cli_prepare.register(commands)
    cli_weekly.register(commands)
    args = parser.parse_args(argv)
    if args.command in cli_weekly.COMMANDS:
        return cli_weekly.run_command(args)
    if args.command in cli_prepare.COMMANDS:
        return cli_prepare.run_command(args, run)

    changes = git_changes(run)
    if args.command == "verdict":
        return _verdict(args, changes, run)
    rows = ledger.read(ledger.verdicts_path())
    if args.command == "carry":
        return _carry(changes, rows, run)
    if args.command == "list":
        return _list(changes, rows)
    pending = unjudged(changes, rows)
    if args.command == "brief":
        print(judge_brief(pending) if pending else "0 pending")
        return 0
    for change in pending:
        print(_line(change))
    print(f"{len(pending)} pending of {len(changes)} fork changes")
    return 0
