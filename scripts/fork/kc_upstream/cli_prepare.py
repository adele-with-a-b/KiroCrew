"""Commands for making fixes ready, asking and sending: ``item``, ``evidence``, ``batch``,
``answer``, ``ask-again``, ``reask``, ``page``, ``status`` and ``send``. Only ``send``
writes to the original, and only what a recorded answer covers."""

from __future__ import annotations

import argparse

from . import batch, page, prepare, send
from .changes import Run, fork_slug

COMMANDS = (
    *("item", "evidence", "batch", "answer", "ask-again", "reask", "page", "status", "send"),
)


def register(commands: argparse._SubParsersAction) -> None:
    item = commands.add_parser("item", help="show one prepared fix's checks")
    item.add_argument("slug")
    evidence = commands.add_parser("evidence", help="record what you ran and saw for a fix")
    evidence.add_argument("slug")
    evidence.add_argument("name", choices=tuple(prepare.EVIDENCE))
    evidence.add_argument("result", choices=prepare.RESULTS)
    evidence.add_argument("--detail", required=True, help="one line: the command and its result")
    made = commands.add_parser("batch", help="freeze the ready fixes into one question")
    made.add_argument("slugs", nargs="*", help="default: every ready fix no batch holds")
    answer = commands.add_parser("answer", help="record the owner's answer; sends nothing")
    answer.add_argument("batch", type=int)
    answer.add_argument("--words", required=True, help="the answer exactly as it was typed")
    again = commands.add_parser("ask-again", help="free a fix that was kept back")
    again.add_argument("batch", type=int)
    again.add_argument("fix", type=int)
    replaced = commands.add_parser("reask", help="replace a batch that changed; frees its fixes")
    replaced.add_argument("batch", type=int)
    door = commands.add_parser("send", help="file what a batch's recorded answer covers")
    door.add_argument("batch", type=int)
    door.add_argument("--dry-run", action="store_true", help="print what would be filed")
    drawn = commands.add_parser("page", help="draw a batch's page again")
    drawn.add_argument("batch", type=int)
    commands.add_parser("status", help="one line: what is being made ready, what waits")


def _owner(run: Run) -> str:
    return fork_slug(run).split("/")[0]


def _item(slug: str, run: Run) -> int:
    checks = prepare.item_checks(prepare.load(slug), prepare.personal_markers(_owner(run)), run)
    for check in checks:
        mark = "note" if check.note else ("ok" if check.ok else "NO")
        print(f"{mark:<5}{check.label}: {check.detail}")
    ready = all(check.ok for check in checks)
    print("ready" if ready else "not ready")
    return 0 if ready else 1


def _batch(slugs: list[str], run: Run) -> int:
    made = batch.make(slugs, _owner(run), run=run)
    count = len(made["items"])
    print(f"batch {made['batch']}: {count} fix(es), code {made['code']}")
    print(f"page: {page.write(made['batch'])}")
    print(f"reply: {batch.reply(made['batch'], made['code'], list(range(1, count + 1)), count)}")
    return 0


def _send(number: int, dry_run: bool, run: Run) -> int:
    try:
        result = send.send(number, dry_run=dry_run, run=run)
    except (RuntimeError, ValueError) as error:
        if not dry_run and number in batch.numbers():
            print(f"page: {page.write(number)}")
        if isinstance(error, RuntimeError):
            raise SystemExit(f"stopped partway: {error}. `send {number}` carries on.") from error
        raise
    if not dry_run:
        left = len(result["approved"]) - len(result["sent"])
        print(f"batch {number}: {len(result['sent'])} sent, {left} left")
        print(f"page: {page.write(number)}")
    return 0


def run_command(args: argparse.Namespace, run: Run) -> int:
    try:
        if args.command == "item":
            return _item(args.slug, run)
        if args.command == "evidence":
            at_head = prepare.head(prepare.git_at(prepare.load(args.slug).worktree, run))
            prepare.record_evidence(args.slug, args.name, args.result, args.detail, at_head)
            print(f"{args.result}  {args.slug}  {args.name}  at {at_head[:10]}")
        elif args.command == "batch":
            return _batch(args.slugs, run)
        elif args.command == "answer":
            row = batch.answer(args.batch, args.words, run=run)
            sent = ", ".join(str(n) for n in row["send"]) or "none"
            print(f"batch {args.batch}: recorded, to send: {sent}. Nothing is sent.")
            print(f"page: {page.write(args.batch)}")
        elif args.command == "ask-again":
            slug = batch.ask_again(args.batch, args.fix)
            print(f"{slug} is free for the next batch")
            print(f"page: {page.write(args.batch)}")
        elif args.command == "reask":
            freed = batch.reask(args.batch)
            print(f"batch {args.batch} is replaced; free for the next batch: {', '.join(freed)}")
            print(f"page: {page.write(args.batch)}")
        elif args.command == "send":
            return _send(args.batch, args.dry_run, run)
        elif args.command == "page":
            print(page.write(args.batch))
        else:
            print(batch.status_line(_owner(run), run=run))
    except ValueError as error:
        raise SystemExit(str(error)) from error
    return 0
