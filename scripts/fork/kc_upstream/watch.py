"""What a scheduled job calls: is any fork change still without a verdict?

``pending_notice`` reads the fork from GitHub's public API, so the job needs no clone
and no credential. It answers with the text to send and a key for the set it found;
the caller sends the text and then calls ``record_notice`` with that key, so a failed
send is tried again on the next run. The same set is announced once a day at most.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from . import UPSTREAM, ledger
from .changes import Change, Fetch, api_changes, http_json

REPEAT_SECONDS = 24 * 60 * 60


def unjudged(changes: list[Change], rows: list[dict]) -> list[Change]:
    judged = ledger.latest(rows)
    return [change for change in changes if change.sha not in judged]


def judge_brief(pending: list[Change], upstream: str = UPSTREAM) -> str:
    lines = [
        f"{len(pending)} change(s) merged into the fork have no upstream verdict.",
        "Dispatch one worker with this brief:",
        "",
        f"Judge each change below for the original project ({upstream}). Work in a checkout",
        "of the fork. First run `python3 scripts/fork/kc_upstream carry`: it copies the",
        "verdict of any change that only moved to a new commit. For each change still listed",
        "by `python3 scripts/fork/kc_upstream pending`: read its diff",
        "(`git diff <sha>^1 <sha>`), check whether the original's main already has it or has",
        "an open pull request or issue for it (read-only `gh`), then record one verdict:",
        "",
        "  python3 scripts/fork/kc_upstream verdict <sha> <VERDICT> --why '<one line>' \\",
        "    --strip '<what to remove before sending, or none>' \\",
        "    --upstream-match '<link, or none>'",
        "",
        "UPSTREAM: any user of the original gains from it as it is.",
        "SPLIT: a generic part is worth sending; a personal part stays in the fork.",
        "FORK-ONLY: it stays in the fork: it is personal, or the original would not take it",
        "  as it is (say in --why what would change that).",
        "ALREADY-UPSTREAM: the original already has it.",
        "",
        f"Write nothing to {upstream}: no pull request, issue, comment or reaction.",
        "Done when `python3 scripts/fork/kc_upstream pending` lists nothing.",
        "",
    ]
    for change in pending:
        number = f" (PR #{change.pr})" if change.pr else ""
        lines.append(f"- {change.sha[:10]} [{change.branch}] {change.subject}{number}")
    return "\n".join(lines)


def pending_notice(
    fork: str,
    seen_path: Path,
    *,
    upstream: str = UPSTREAM,
    fetch: Fetch = http_json,
    now: Callable[[], float] = time.time,
) -> Optional[tuple[str, str]]:
    """``(text, key)`` when there is something new to say, else ``None``."""
    pending = unjudged(api_changes(fork, upstream, fetch), ledger.read(ledger.verdicts_path()))
    if not pending:
        return None
    key = ",".join(sorted(change.sha for change in pending))
    try:
        seen = json.loads(seen_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    if seen.get("key") == key and now() - float(seen.get("at", 0)) < REPEAT_SECONDS:
        return None
    return judge_brief(pending, upstream), key


def record_notice(seen_path: Path, key: str, *, now: Callable[[], float] = time.time) -> None:
    seen_path.write_text(json.dumps({"key": key, "at": now()}) + "\n", encoding="utf-8")
