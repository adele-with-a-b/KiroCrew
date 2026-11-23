"""One fix made ready for the original project: its texts, its branch, its checks.

An item is a folder ``items/<slug>/`` in the state directory:

- ``item.json``: ``title`` (the fix in plain words), ``wrong`` (what is wrong today),
  ``who`` (who it helps), ``worktree`` (a checkout of the fix's branch), ``changes``
  (the fork commits it comes from).
- ``issue.md`` and ``pr.md``: the exact texts to file. The first line is ``# <title>``.
  The pull request text carries one ``Closes {{issue}}`` line: the issue's number is
  only known once the issue exists.
- ``evidence.jsonl``: what the worker ran and saw, each row tied to the branch's commit.

Nothing here writes to the original project. ``item_checks`` only reads.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import ledger
from .changes import Run, run_git

ISSUE_SLOT = "Closes {{issue}}"
BASE = "upstream/main"
FORM_SCRIPT = ".github/scripts/pr-description-check.sh"

# What only the worker can show. Each is recorded with `evidence` and holds for the
# commit it was recorded on: a new commit on the branch needs a new run.
EVIDENCE = {
    "still-broken": "Still broken in the original",
    "tests": "Tests",
    "real-machine": "Tried on a real machine",
    "fails-without-fix": "Test fails without the fix",
}

RESULTS = ("ok", "note", "fail")

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
_HOME_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")
_UNFILLED = re.compile(
    r"<to fill|<command>|<output>|<issue line|\bTBD\b|will fill|will attach|forthcoming"
    r"|\{\{(?!issue\}\})",
    re.IGNORECASE,
)

Form = Callable[[Path, str], Optional[str]]


@dataclass(frozen=True)
class Check:
    name: str
    label: str
    ok: bool
    detail: str
    note: bool = False  # passes, with something the reader should know


@dataclass(frozen=True)
class Item:
    slug: str
    title: str
    wrong: str
    who: str
    worktree: Path
    changes: tuple[str, ...]
    issue_title: str
    issue_body: str
    pr_title: str
    pr_body: str


def items_dir() -> Path:
    return ledger.state_dir() / "items"


def item_dir(slug: str) -> Path:
    if not _SLUG.fullmatch(slug):
        raise ValueError(f"{slug!r}: an item name is lowercase letters, digits and dashes")
    return items_dir() / slug


def slugs() -> list[str]:
    root = items_dir()
    return (
        sorted(p.name for p in root.iterdir() if (p / "item.json").exists())
        if root.exists()
        else []
    )


def split_text(text: str, where: str) -> tuple[str, str]:
    """``# title`` on the first line, the body after it."""
    first, _, rest = text.partition("\n")
    if not first.startswith("# ") or not first[2:].strip():
        raise ValueError(f"{where}: the first line must be '# <title>'")
    return first[2:].strip(), rest.strip() + "\n"


def load(slug: str) -> Item:
    folder = item_dir(slug)
    try:
        meta = json.loads((folder / "item.json").read_text(encoding="utf-8"))
        issue = split_text((folder / "issue.md").read_text(encoding="utf-8"), f"{slug}/issue.md")
        pull = split_text((folder / "pr.md").read_text(encoding="utf-8"), f"{slug}/pr.md")
        return Item(
            slug=slug,
            title=str(meta["title"]),
            wrong=str(meta["wrong"]),
            who=str(meta["who"]),
            worktree=Path(meta["worktree"]),
            changes=tuple(meta.get("changes", ())),
            issue_title=issue[0],
            issue_body=issue[1],
            pr_title=pull[0],
            pr_body=pull[1],
        )
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise ValueError(f"item {slug}: {error!r}") from error


def git_at(worktree: Path, run: Run = run_git) -> Run:
    """``run`` bound to one checkout."""

    def bound(args: list[str], stdin: Optional[str] = None) -> str:
        return run(["-C", str(worktree), *args], stdin)

    return bound


def head(git: Run) -> str:
    return git(["rev-parse", "HEAD"]).strip()


def branch_patch_id(git: Run) -> str:
    """The id of the branch's diff against the original; a plain rebase keeps it."""
    diff = git(["diff", f"{BASE}...HEAD"])
    return git(["patch-id", "--stable"], diff).split()[0]


def personal_markers(fork_owner: str) -> list[str]:
    """Words that must not reach the original: the fork's owner, plus ``markers.txt``."""
    found = [fork_owner] if fork_owner else []
    path = ledger.state_dir() / "markers.txt"
    if path.exists():
        found += [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [marker for marker in found if marker and not marker.startswith("#")]


def personal_hits(text: str, markers: list[str]) -> list[str]:
    low = text.lower()
    hits = [marker for marker in markers if marker.lower() in low]
    return hits + sorted(set(_HOME_PATH.findall(text)))


def run_form(worktree: Path, body: str) -> Optional[str]:
    """The original's own pull request form check, or ``None`` when it passes."""
    script = worktree / FORM_SCRIPT
    if not script.exists():
        return f"{FORM_SCRIPT} is not in the checkout"
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "output"
        env = dict(os.environ, PR_BODY=body, PR_DRAFT="false", GITHUB_OUTPUT=str(out))
        subprocess.run(
            ["bash", str(script)],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
        result = out.read_text(encoding="utf-8") if out.exists() else ""
    if re.search(r"^conclusion=success$", result, re.MULTILINE):
        return None
    note = re.search(r"^annotation=(.*)$", result, re.MULTILINE)
    return note.group(1) if note and note.group(1) else "the form check did not pass"


def _branch(git: Run) -> Check:
    ahead = int(git(["rev-list", "--count", f"{BASE}..HEAD"]).strip())
    behind = int(git(["rev-list", "--count", f"HEAD..{BASE}"]).strip())
    if ahead != 1:
        return Check(
            "branch", "Clean branch", False, f"{ahead} commits on top of {BASE}; make it 1."
        )
    if behind == 0:
        return Check("branch", "Clean branch", True, "One commit on the original's newest main.")
    try:
        git(["merge-tree", "--write-tree", BASE, "HEAD"])
    except RuntimeError:
        detail = f"It no longer applies on the original's main ({behind} commits newer): rebase it."
        return Check("branch", "Clean branch", False, detail)
    detail = (
        f"One commit; it still applies cleanly on the original's main ({behind} commits newer)."
    )
    return Check("branch", "Clean branch", True, detail)


def _names(item: Item, git: Run, markers: list[str]) -> Check:
    added = "\n".join(
        line[1:]
        for line in git(["diff", f"{BASE}...HEAD"]).splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    parts = {
        "the code": added,
        "the commit message": git(["log", "-1", "--format=%B"]),
        "the issue": f"{item.issue_title}\n{item.issue_body}",
        "the pull request": f"{item.pr_title}\n{item.pr_body}",
    }
    found = [
        f"{where}: {', '.join(hits)}"
        for where, text in parts.items()
        if (hits := personal_hits(text, markers))
    ]
    detail = (
        "; ".join(found)
        if found
        else "None in the code, the commit, the issue or the pull request."
    )
    return Check("names", "Your names and paths", not found, detail)


def _form(item: Item, form: Form) -> Check:
    problems = []
    for where, text in (("issue", item.issue_body), ("pull request", item.pr_body)):
        unfilled = sorted(set(m.group(0) for m in _UNFILLED.finditer(text)))
        if unfilled:
            problems.append(f"the {where} has unfilled parts: {', '.join(unfilled)}")
    slots = [line for line in item.pr_body.splitlines() if line.strip() == ISSUE_SLOT]
    if len(slots) != 1 or item.pr_body.count("{{issue}}") != 1:
        problems.append(f"the pull request needs exactly one line '{ISSUE_SLOT}'")
    refused = form(item.worktree, item.pr_body.replace("{{issue}}", "#1"))
    if refused:
        problems.append(refused)
    detail = (
        "; ".join(problems) if problems else "Every part filled in; their own form check passes."
    )
    return Check("form", "Their pull request form", not problems, detail)


def evidence_path(slug: str) -> Path:
    return item_dir(slug) / "evidence.jsonl"


def record_evidence(
    slug: str,
    name: str,
    result: str,
    detail: str,
    at_head: str,
    *,
    now: Callable[[], float] = time.time,
) -> None:
    """``result`` is ``ok``, ``note`` (passes, and the detail says what to know) or ``fail``."""
    if name not in EVIDENCE:
        raise ValueError(f"{name!r}: evidence is one of {', '.join(EVIDENCE)}")
    if not detail.strip():
        raise ValueError("evidence needs a one-line account of what was run and seen")
    if result not in RESULTS:
        raise ValueError(f"{result!r}: a result is one of {', '.join(RESULTS)}")
    row = {"name": name, "result": result, "detail": detail.strip(), "head": at_head, "at": now()}
    with evidence_path(slug).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _evidence(slug: str, at_head: str) -> list[Check]:
    latest = {row.get("name"): row for row in ledger.read(evidence_path(slug))}
    checks = []
    for name, label in EVIDENCE.items():
        row = latest.get(name)
        if row is None:
            checks.append(Check(name, label, False, "Not run yet."))
        elif row.get("head") != at_head:
            checks.append(Check(name, label, False, "Run on an older commit; run it again."))
        else:
            result = row.get("result")
            checks.append(
                Check(
                    name,
                    label,
                    result in ("ok", "note"),
                    str(row.get("detail", "")),
                    result == "note",
                )
            )
    return checks


def item_checks(
    item: Item, markers: list[str], run: Run = run_git, form: Optional[Form] = None
) -> list[Check]:
    """Every check for one item, in the order the page shows them."""
    git = git_at(item.worktree, run)
    by_name = {check.name: check for check in _evidence(item.slug, head(git))}
    return [
        by_name["still-broken"],
        _branch(git),
        _names(item, git, markers),
        by_name["tests"],
        _form(item, form or run_form),
        by_name["real-machine"],
        by_name["fails-without-fix"],
    ]


def files_changed(git: Run) -> int:
    return len(git(["diff", "--name-only", f"{BASE}...HEAD"]).split())
