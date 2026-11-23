"""A batch: the ready fixes put to the fork's owner as one question, and the answer.

``make`` freezes the exact texts into ``batches/<n>/`` and gives the batch a code: the
start of a hash over every issue text, every pull request text and every branch's diff.
The owner answers in chat with words that carry the code (``send batch 1 386c``,
``send batch 1 386c, only 1 and 2`` or ``send none``). ``answer`` takes those words as
they were typed and records a yes only when the code still matches what is on disk, so
a text or a diff changed after the question cannot ride on an old yes.

A recorded yes sends nothing. Sending is a separate command that reads this record.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from . import UPSTREAM, ledger, prepare
from .changes import Run, run_git

SEND_VERDICTS = ("UPSTREAM", "SPLIT")
_ANSWER = re.compile(r"send batch (\d+) ([0-9a-f]{4})(?:,? only (\d+(?:(?:, and | and |, )\d+)*))?")

Clock = Callable[[], float]


def batches_dir() -> Path:
    return ledger.state_dir() / "batches"


def batch_dir(number: int) -> Path:
    return batches_dir() / str(number)


def answers_path() -> Path:
    return ledger.state_dir() / "approvals.jsonl"


def numbers() -> list[int]:
    root = batches_dir()
    if not root.exists():
        return []
    return sorted(int(p.name) for p in root.iterdir() if (p / "manifest.json").exists())


def code_of(parts: list[tuple[str, str, str, str, str]]) -> str:
    """Four hex digits over (issue title, issue text, PR title, PR text, diff id) per fix."""
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()[:4]


def manifest(number: int) -> dict:
    try:
        return json.loads((batch_dir(number) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"batch {number}: {error!r}") from error


def frozen_texts(number: int, position: int) -> tuple[str, str, str, str]:
    """(issue title, issue text, PR title, PR text) exactly as the owner was shown them."""
    folder = batch_dir(number)
    issue = prepare.split_text(
        (folder / f"{position}-issue.md").read_text(encoding="utf-8"), f"{position}-issue.md"
    )
    pull = prepare.split_text(
        (folder / f"{position}-pr.md").read_text(encoding="utf-8"), f"{position}-pr.md"
    )
    return (*issue, *pull)


def current_code(number: int, run: Run = run_git) -> str:
    """The batch's code from what is on disk and in the branches right now."""
    parts = []
    for entry in manifest(number)["items"]:
        diff_id = prepare.branch_patch_id(prepare.git_at(Path(entry["worktree"]), run))
        parts.append((*frozen_texts(number, entry["n"]), diff_id))
    return code_of(parts)


def _rows() -> list[dict]:
    return ledger.read(answers_path())


def answer_of(number: int, rows: Optional[list[dict]] = None) -> Optional[dict]:
    for row in _rows() if rows is None else rows:
        if row.get("kind") == "answer" and row.get("batch") == number:
            return row
    return None


def asked_again(number: int, rows: Optional[list[dict]] = None) -> set[int]:
    return {
        int(row["item"])
        for row in (_rows() if rows is None else rows)
        if row.get("kind") == "ask-again" and row.get("batch") == number
    }


def held_slugs() -> dict[str, str]:
    """Items a batch still holds, with why: they cannot go into a new batch."""
    rows = _rows()
    held = {}
    for number in numbers():
        answer = answer_of(number, rows)
        again = asked_again(number, rows)
        for entry in manifest(number)["items"]:
            if answer is None:
                held[entry["slug"]] = f"batch {number} is waiting for an answer"
            elif entry["n"] in answer["send"]:
                held[entry["slug"]] = f"batch {number} has a yes for it"
            elif entry["n"] not in again:
                held[entry["slug"]] = (
                    f"kept back in batch {number}; `ask-again {number} {entry['n']}` frees it"
                )
    return held


def _unjudged(item: prepare.Item) -> list[str]:
    judged = ledger.latest(ledger.read(ledger.verdicts_path()))
    bad = []
    for sha in item.changes:
        hits = [row for full, row in judged.items() if full.startswith(sha)]
        if len(hits) != 1 or hits[0]["verdict"] not in SEND_VERDICTS:
            bad.append(sha)
    return bad


def make(
    names: list[str],
    fork_owner: str,
    *,
    run: Run = run_git,
    form: Optional[prepare.Form] = None,
    now: Clock = time.time,
) -> dict:
    """Freeze the named items (or every ready, free one) into the next batch."""
    held = held_slugs()
    markers = prepare.personal_markers(fork_owner)
    chosen, refused = [], []
    for slug in names or [slug for slug in prepare.slugs() if slug not in held]:
        item = prepare.load(slug)
        checks = prepare.item_checks(item, markers, run, form)
        failing = [check.label for check in checks if not check.ok]
        if slug in held:
            refused.append(f"{slug}: {held[slug]}")
        elif not item.changes or _unjudged(item):
            refused.append(f"{slug}: every fork change it comes from needs a verdict to send")
        elif failing:
            if names:
                refused.append(f"{slug}: not ready ({', '.join(failing)})")
        else:
            chosen.append((item, checks))
    if refused:
        raise ValueError("; ".join(refused))
    if not chosen:
        raise ValueError("no item is ready and free")

    number = max(numbers(), default=0) + 1
    folder = batch_dir(number)
    folder.mkdir(parents=True)
    entries, parts = [], []
    for position, (item, checks) in enumerate(chosen, start=1):
        git = prepare.git_at(item.worktree, run)
        diff_id = prepare.branch_patch_id(git)
        (folder / f"{position}-issue.md").write_text(
            f"# {item.issue_title}\n\n{item.issue_body}", encoding="utf-8"
        )
        (folder / f"{position}-pr.md").write_text(
            f"# {item.pr_title}\n\n{item.pr_body}", encoding="utf-8"
        )
        parts.append((*frozen_texts(number, position), diff_id))
        entries.append(
            {
                "n": position,
                "slug": item.slug,
                "title": item.title,
                "wrong": item.wrong,
                "who": item.who,
                "worktree": str(item.worktree),
                "changes": list(item.changes),
                "head": prepare.head(git),
                "patch_id": diff_id,
                "files": prepare.files_changed(git),
                "checks": [asdict(check) for check in checks],
            }
        )
    made = {
        "batch": number,
        "code": code_of(parts),
        "made_at": now(),
        "account": fork_owner,
        "upstream": UPSTREAM,
        "items": entries,
    }
    (folder / "manifest.json").write_text(json.dumps(made, indent=2) + "\n", encoding="utf-8")
    return made


def reply(number: int, code: str, send: list[int], count: int) -> str:
    """The words that mean "send these": what the page shows and ``answer`` reads."""
    if not send:
        return "send none"
    base = f"send batch {number} {code}"
    if len(send) == count:
        return base
    listed = [str(n) for n in send]
    tail = listed[0] if len(listed) == 1 else f"{', '.join(listed[:-1])} and {listed[-1]}"
    return f"{base}, only {tail}"


def read_answer(words: str, number: int, code: str, count: int) -> list[int]:
    """Which fixes the owner's words send. Anything but the exact forms is refused."""
    said = " ".join(words.strip().strip(".").lower().split())
    if said == "send none":
        return []
    match = _ANSWER.fullmatch(said)
    if match is None:
        raise ValueError(
            f"not an answer: {words!r}. It is '{reply(number, code, [1], 1)}', "
            f"'{reply(number, code, [1], 2)}' (with the numbers to send) or 'send none'"
        )
    if int(match.group(1)) != number:
        raise ValueError(f"those words answer batch {match.group(1)}, not batch {number}")
    if match.group(2) != code:
        raise ValueError(f"the code {match.group(2)} is not batch {number}'s code")
    if match.group(3) is None:
        return list(range(1, count + 1))
    send = sorted({int(n) for n in re.findall(r"\d+", match.group(3))})
    if not send or send[0] < 1 or send[-1] > count:
        raise ValueError(f"batch {number} has fixes 1 to {count}")
    return send


def answer(number: int, words: str, *, run: Run = run_git, now: Clock = time.time) -> dict:
    """Record the owner's answer to a batch. Nothing is sent."""
    made = manifest(number)
    if answer_of(number) is not None:
        raise ValueError(f"batch {number} already has an answer")
    live = current_code(number, run)
    if live != made["code"]:
        raise ValueError(
            f"batch {number} changed after it was shown (its code is now {live}, it was "
            f"{made['code']}): make a new batch and ask again"
        )
    send = read_answer(words, number, made["code"], len(made["items"]))
    row = {
        "kind": "answer",
        "batch": number,
        "code": made["code"],
        "send": send,
        "words": words,
        "at": now(),
    }
    _append(row)
    return row


def ask_again(number: int, position: int, *, now: Clock = time.time) -> str:
    """Free a fix the owner kept back, so the next batch can ask about it again."""
    made = manifest(number)
    given = answer_of(number)
    entry = next((e for e in made["items"] if e["n"] == position), None)
    if entry is None:
        raise ValueError(f"batch {number} has no fix {position}")
    if given is None:
        raise ValueError(f"batch {number} is still waiting for its first answer")
    if position in given["send"]:
        raise ValueError(f"fix {position} of batch {number} already has a yes")
    if position in asked_again(number):
        raise ValueError(f"fix {position} of batch {number} is already free")
    _append({"kind": "ask-again", "batch": number, "item": position, "at": now()})
    return entry["slug"]


def _append(row: dict) -> None:
    path = answers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def status_line(fork_owner: str, *, run: Run = run_git, form: Optional[prepare.Form] = None) -> str:
    """One line for a daily message: what is being made ready and what waits on a yes."""
    held = held_slugs()
    markers = prepare.personal_markers(fork_owner)
    ready = getting = 0
    for slug in prepare.slugs():
        if slug in held:
            continue
        checks = prepare.item_checks(prepare.load(slug), markers, run, form)
        if all(check.ok for check in checks):
            ready += 1
        else:
            getting += 1
    rows = _rows()
    waiting = [n for n in numbers() if answer_of(n, rows) is None]
    unsent = [n for n in numbers() if (a := answer_of(n, rows)) and a["send"]]
    parts = []
    if getting:
        parts.append(f"{getting} being made ready")
    if ready:
        parts.append(f"{ready} ready for the next batch")
    parts += [f"batch {n} waiting for a yes" for n in waiting]
    parts += [f"batch {n} has a yes, not sent yet" for n in unsent]
    return "Fixes for the original: " + ("; ".join(parts) if parts else "nothing in progress") + "."
