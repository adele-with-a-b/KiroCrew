"""What the send door did: one append-only JSON-lines file, ``sent.jsonl``.

A row is one finished step for one fix of one batch: ``issue`` and ``pr`` carry the
link and the number, ``push`` the branch. A ``stopped`` row says a batch's yes stopped
counting, and why. The pages and the door's own resume read nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import ledger


def path() -> Path:
    return ledger.state_dir() / "sent.jsonl"


def rows(number: int) -> list[dict]:
    return [row for row in ledger.read(path()) if row.get("batch") == number]


def step_of(found: list[dict], position: int, step: str) -> Optional[dict]:
    for row in found:
        if row.get("n") == position and row.get("step") == step:
            return row
    return None


def sent_positions(found: list[dict]) -> set[int]:
    """The fixes whose pull request is out."""
    return {int(row["n"]) for row in found if row.get("step") == "pr"}


def stopped(found: list[dict]) -> Optional[dict]:
    for row in found:
        if row.get("step") == "stopped":
            return row
    return None


def record(batch: int, step: str, at: float, /, **fields: object) -> dict:
    row = {"batch": batch, "step": step, "at": at, **fields}
    where = path()
    where.parent.mkdir(parents=True, exist_ok=True)
    with where.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


def issue_awaiting_pull(slug: str, number: int, repo: str) -> Optional[dict]:
    """The issue an earlier batch filed on ``repo`` for this item, its pull request never made."""
    home = f"https://github.com/{repo}/"
    every = ledger.read(path())
    done = {(row["batch"], row["n"]) for row in every if row.get("step") == "pr"}
    found = None
    for row in every:
        if row.get("step") != "issue" or row.get("slug") != slug or row["batch"] == number:
            continue
        if (row["batch"], row["n"]) not in done and str(row.get("url", "")).startswith(home):
            found = row
    return found
