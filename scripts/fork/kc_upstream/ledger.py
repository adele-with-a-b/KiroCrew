"""The verdict record: one append-only JSON-lines file outside the repository.

A lost or unreadable file makes every change read as unjudged, never as judged.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

VERDICTS = ("UPSTREAM", "SPLIT", "FORK-ONLY", "ALREADY-UPSTREAM")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def state_dir() -> Path:
    override = os.environ.get("KC_UPSTREAM_STATE")
    return Path(override) if override else Path.home() / ".local" / "state" / "kc-upstream"


def verdicts_path() -> Path:
    return state_dir() / "verdicts.jsonl"


def read(path: Path) -> list[dict]:
    """Every row, oldest first. A line that is not a JSON object is an error, not a skip."""
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{number}: not JSON ({error.msg})") from error
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{number}: not a JSON object")
        rows.append(row)
    return rows


def append(path: Path, row: dict) -> None:
    if not _FULL_SHA.fullmatch(str(row.get("sha", ""))):
        raise ValueError("sha must be a full 40-character commit id")
    if row.get("verdict") not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    if not str(row.get("why", "")).strip():
        raise ValueError("why must say, in one line, what decided the verdict")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def latest(rows: list[dict]) -> dict[str, dict]:
    """The newest row per commit: a change judged again keeps only its last verdict."""
    return {str(row["sha"]): row for row in rows if "sha" in row}
