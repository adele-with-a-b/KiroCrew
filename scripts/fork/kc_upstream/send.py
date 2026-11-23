"""The one door that files on the original project: ``send <batch>``.

It files only what a recorded yes covers, and only while that yes still stands. Before
anything goes out, every fix the owner said to send is compared with what they read: the
saved texts and the diff (the batch code), the item's own texts, the commit, and the
personal words on the markers list. One difference in a fix that is still to go stops the
batch; it is written down, nothing more is filed on that yes, and ``reask`` makes the
new question. A fix the owner kept back, or one already out, is not looked at.

Then, per fix and in order: the branch pushed to the fork, the issue, the pull request
whose text names that issue. Each finished step is written to the journal at once. Before
filing, the door looks for what is already there: an issue an earlier batch filed for the
same item without reaching its pull request (it is reused, and brought to the approved
text), an issue of this yes whose reply was lost, an open pull request from the branch
(taken only when it is word for word this one). So a run that stopped halfway, or a batch
asked again after it, carries on without filing anything twice.

What the door prints is read by an agent. It prints its own texts, links, and the error
text of ``git`` and ``gh``. From the original it prints the links it fetched, never a title
or a body.

``dry_run`` goes through the same checks and prints what would be filed, in order. It
writes nothing, here or there.
"""

from __future__ import annotations

import difflib
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import batch, journal, prepare
from .changes import Run, fork_slug, run_git

Say = Callable[[str], None]
ISSUE_NUMBER_PENDING = "#<the number of the issue above>"
BRANCH_PREFIX = "up/"
_CLOCK_SLACK = 300.0
_DIFF_LINES = 40


class Stopped(ValueError):
    """The batch is no longer what the owner said yes to."""


def run_gh(args: list[str], stdin: Optional[str] = None) -> str:
    done = subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {done.stderr.strip()}")
    return done.stdout


def _text_diff(shown: tuple[str, ...], now: tuple[str, ...]) -> str:
    lines: list[str] = []
    for name, before, after in zip(("issue", "issue", "pull request", "pull request"), shown, now):
        if before != after:
            found = difflib.unified_diff(
                before.splitlines(), after.splitlines(), f"{name}, as shown", f"{name}, now", n=1
            )
            lines += [line.rstrip("\n") for line in found]
    return "\n".join(lines[:_DIFF_LINES])


def changes_since_yes(number: int, made: dict, positions: list[int], run: Run) -> list[dict]:
    """Every way the fixes still to go differ from what was shown; empty while the yes stands.

    A row is ``{"n": fix, "why": ...}``; ``n`` is 0 only for a batch made before fixes had
    their own code, where a changed saved copy cannot be pinned on one fix.
    """
    found: list[dict] = []
    markers = prepare.personal_markers(made["account"])
    todo = [entry for entry in made["items"] if entry["n"] in positions]
    for entry in todo:
        n = entry["n"]
        git = prepare.git_at(Path(entry["worktree"]), run)
        try:
            texts = batch.frozen_texts(number, n)
        except (OSError, ValueError) as error:
            found.append({"n": n, "why": f"its saved texts cannot be read ({error})"})
            continue
        if "code" in entry and batch.code_of([(*texts, entry["patch_id"])]) != entry["code"]:
            found.append({"n": n, "why": "its saved texts were changed"})
        try:
            item = prepare.load(entry["slug"])
            current = (item.issue_title, item.issue_body, item.pr_title, item.pr_body)
            if current != texts:
                found.append(
                    {
                        "n": n,
                        "why": "the issue or pull request text was edited",
                        "diff": _text_diff(texts, current),
                    }
                )
        except ValueError:
            found.append({"n": n, "why": "its item is gone"})
        if prepare.branch_patch_id(git) != entry["patch_id"]:
            found.append({"n": n, "why": "the code change is different"})
        elif prepare.head(git) != entry["head"]:
            found.append({"n": n, "why": "the branch has a different commit"})
        message = git(["log", "-1", "--format=%B"])
        hits = prepare.personal_hits(
            "\n".join((*texts, prepare.added_lines(git), message)), markers
        )
        hits += prepare.marker_hits(prepare.published_names(git), markers)
        if hits:
            found.append({"n": n, "why": f"a personal word is in it: {', '.join(hits)}"})
    if todo and not found and not all("code" in entry for entry in made["items"]):
        live = batch.current_code(number, run)
        if live != made["code"]:
            found.append({"n": 0, "why": f"the saved texts were changed (the code is now {live})"})
    return found


def _number(url: str) -> int:
    return int(url.strip().rstrip("/").rsplit("/", 1)[1])


def _existing_issue(
    gh: Run, repo: str, account: str, title: str, since: float, taken: set[str]
) -> Optional[str]:
    """An issue this account opened with this exact title after the yes, if there is one."""
    listed = json.loads(
        gh(["api", f"repos/{repo}/issues?creator={account}&state=all&per_page=100"])
    )
    for found in listed:
        if "pull_request" in found or found.get("title") != title:
            continue
        made = datetime.fromisoformat(str(found["created_at"]).replace("Z", "+00:00"))
        if made.timestamp() >= since - _CLOCK_SLACK and found["html_url"] not in taken:
            return str(found["html_url"])
    return None


def _existing_pull(gh: Run, repo: str, account: str, branch: str) -> Optional[dict]:
    listed = json.loads(gh(["api", f"repos/{repo}/pulls?head={account}:{branch}&state=open"]))
    return listed[0] if listed else None


def _same_text(one: Optional[str], other: str) -> bool:
    return (one or "").replace("\r\n", "\n").rstrip() == other.rstrip()


def _describe(found: list[dict]) -> str:
    return "; ".join((f"fix {row['n']}: " if row["n"] else "") + row["why"] for row in found)


def _earlier_issue(
    entry: dict, number: int, repo: str, title: str, body: str
) -> tuple[Optional[dict], bool]:
    """(the issue an earlier batch filed for this item, whether its text is still this one)."""
    row = journal.issue_awaiting_pull(entry["slug"], number, repo)
    if row is None:
        return None, True
    try:
        same = batch.frozen_texts(row["batch"], row["n"])[:2] == (title, body)
    except (OSError, ValueError):
        same = False
    return row, same


def send(
    number: int,
    *,
    dry_run: bool = False,
    run: Run = run_git,
    gh: Optional[Run] = None,
    now: Callable[[], float] = time.time,
    say: Say = print,
) -> dict:
    """File the fixes batch ``number``'s answer covers. Returns the fixes now out."""
    gh = gh or run_gh
    made = batch.manifest(number)
    answer = batch.answer_of(number)
    if batch.voided(number):
        raise ValueError(f"batch {number} was replaced by a newer question: nothing may be sent")
    if answer is None:
        if not dry_run:
            raise ValueError(f"batch {number} has no answer: nothing may be sent")
        say(f"batch {number} has no answer yet. This is what a yes to all of it would file.")
        approved, since = [entry["n"] for entry in made["items"]], now()
    else:
        if answer.get("code") != made["code"]:
            raise ValueError(f"batch {number}'s answer carries another code: nothing may be sent")
        approved, since = list(answer["send"]), float(answer["at"])
        if not approved:
            raise ValueError(f"the answer to batch {number} was 'send none': nothing may be sent")
    done = journal.rows(number)
    out = journal.sent_positions(done)
    todo = [n for n in approved if n not in out]
    if not todo:
        say(f"batch {number}: every fix the answer covers is already sent")
        return {"sent": sorted(out), "approved": approved}
    if journal.stopped(done):
        raise Stopped(f"batch {number} changed after the yes: `reask {number}` asks again")

    repo, account = made["upstream"], made["account"]
    branches: dict[int, tuple[str, str]] = {}
    try:
        login = gh(["api", "user", "--jq", ".login"]).strip()
        changed = changes_since_yes(number, made, todo, run)
        for entry in made["items"]:
            if entry["n"] in todo:
                git = prepare.git_at(Path(entry["worktree"]), run)
                found = (git(["rev-parse", "--abbrev-ref", "HEAD"]).strip(), fork_slug(git))
                branches[entry["n"]] = found
    except RuntimeError as error:
        raise ValueError(
            f"batch {number} could not be checked, so nothing was sent: {error}"
        ) from error
    if login != account:
        raise ValueError(f"gh is signed in as {login!r}; this batch files as {account!r}")
    if changed:
        if not dry_run:
            journal.record(number, "stopped", now(), was=made["code"], fixes=changed)
        raise Stopped(
            f"batch {number} is not what was shown, so nothing"
            f"{' more' if out else ''} is sent: {_describe(changed)}"
        )
    for n, (branch, pushed_to) in branches.items():
        if not branch.startswith(BRANCH_PREFIX) or pushed_to.split("/")[0] != account:
            raise ValueError(
                f"fix {n}: branch {branch!r} of {pushed_to} cannot be sent as {account}"
            )
    if len(set(branches.values())) != len(branches):
        raise ValueError(
            f"batch {number}: two fixes are on one branch, so nothing was sent; give each its own"
        )

    base = prepare.BASE.split("/", 1)[1]
    say(
        f"batch {number} -> {repo} as {account}"
        + (" (dry run: nothing is filed)" if dry_run else "")
    )
    for entry in made["items"]:
        n = entry["n"]
        if n not in todo:
            say(
                f"fix {n} ({entry['slug']}): "
                + ("already sent" if n in out else "kept back, not sent")
            )
            continue
        issue_title, issue_body, pr_title, pr_body = batch.frozen_texts(number, n)
        git = prepare.git_at(Path(entry["worktree"]), run)
        branch, pushed_to = branches[n]
        done = journal.rows(number)
        issue = journal.step_of(done, n, "issue")
        issue_url: Optional[str] = issue["url"] if issue else None
        earlier, same = (
            (None, True) if issue else _earlier_issue(entry, number, repo, issue_title, issue_body)
        )
        if earlier is not None:
            issue_url = earlier["url"]
        elif issue_url is None and answer is not None:
            taken = {row["url"] for row in done if row.get("step") == "issue"}
            issue_url = _existing_issue(gh, repo, account, issue_title, since, taken)
        pull = _existing_pull(gh, repo, account, branch)
        ours = (
            pull is not None
            and issue_url is not None
            and pull.get("title") == pr_title
            and _same_text(pull.get("body"), pr_body.replace("{{issue}}", f"#{_number(issue_url)}"))
        )
        if pull is not None and not ours:
            raise ValueError(
                f"fix {n}: {pull['html_url']} is already open from {branch} and is not this pull "
                "request. Nothing was pushed or filed for this fix; close that one or sort it out "
                "by hand, then send again"
            )

        if dry_run:
            say(f"\n== fix {n}: {entry['slug']}")
            say(f"1. PUSH {entry['head'][:10]} to {pushed_to} as {branch}")
            if issue_url is None:
                say(f"2. ISSUE on {repo}\ntitle: {issue_title}\n\n{issue_body}")
            elif same:
                say(f"2. ISSUE is already there: {issue_url}")
            else:
                say(
                    f"2. ISSUE {issue_url} is brought to this text\ntitle: {issue_title}\n\n{issue_body}"
                )
            closes = f"#{_number(issue_url)}" if issue_url else ISSUE_NUMBER_PENDING
            if pull is not None:
                say(f"3. PULL REQUEST is already there: {pull['html_url']}")
                continue
            say(
                f"3. PULL REQUEST on {repo}, base {base}, head {account}:{branch}\n"
                f"title: {pr_title}\n\n{pr_body.replace('{{issue}}', closes)}"
            )
            continue

        if journal.step_of(done, n, "push") is None:
            force = [] if pull is not None else ["--force"]
            git(["push", *force, "origin", f"{entry['head']}:refs/heads/{branch}"])
            journal.record(
                number, "push", now(), n=n, slug=entry["slug"], branch=branch, to=pushed_to
            )

        if issue_url is None:
            issue_url = gh(
                ["issue", "create", "--repo", repo, "--title", issue_title, "--body-file", "-"],
                issue_body,
            ).strip()
        elif not same:
            gh(
                [
                    *("issue", "edit", str(_number(issue_url)), "--repo", repo),
                    *("--title", issue_title, "--body-file", "-"),
                ],
                issue_body,
            )
        if issue is None:
            journal.record(
                number,
                "issue",
                now(),
                n=n,
                slug=entry["slug"],
                url=issue_url,
                number=_number(issue_url),
            )
        say(f"fix {n}: issue {issue_url}")

        body = pr_body.replace("{{issue}}", f"#{_number(issue_url)}")
        if pull is None:
            head = f"{account}:{branch}"
            pull_url = gh(
                [
                    *("pr", "create", "--repo", repo, "--base", base, "--head", head),
                    *("--title", pr_title, "--body-file", "-"),
                ],
                body,
            ).strip()
        else:
            pull_url = str(pull["html_url"])
        journal.record(
            number, "pr", now(), n=n, slug=entry["slug"], url=pull_url, number=_number(pull_url)
        )
        say(f"fix {n}: pull request {pull_url}")
        out.add(n)
    return {"sent": sorted(out), "approved": approved}
