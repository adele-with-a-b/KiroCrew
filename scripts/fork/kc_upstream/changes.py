"""The fork's own changes: the first-parent commits of ``local/<release>`` after ``v<release>``.

One merged pull request is one change (its merge commit); a commit pushed straight to
the branch is one change too. Two readers give the same list: ``git_changes`` from a
clone, and ``api_changes`` from GitHub's public API, which needs no clone and no token.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

API = "https://api.github.com"
BRANCH_PREFIX = "local/"
_RELEASE_BRANCH = re.compile(r"local/(\d+\.\d+\.\d+)")
_MERGED_PR = re.compile(r"Merge pull request #(\d+) ")

Run = Callable[..., str]
Fetch = Callable[[str], object]


@dataclass(frozen=True)
class Change:
    sha: str
    subject: str
    branch: str
    pr: Optional[int]


def release_tag(branch: str) -> Optional[str]:
    """``local/0.13.0`` stands on ``v0.13.0``; any other branch name has no release."""
    match = _RELEASE_BRANCH.fullmatch(branch)
    return f"v{match.group(1)}" if match else None


def _change(sha: str, subject: str, branch: str) -> Change:
    match = _MERGED_PR.match(subject)
    return Change(sha, subject, branch, int(match.group(1)) if match else None)


def run_git(args: list[str], stdin: Optional[str] = None) -> str:
    done = subprocess.run(
        ["git", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def git_changes(run: Run = run_git, remote: str = "origin") -> list[Change]:
    """Every fork change on the remote's release branches, newest first per branch."""
    prefix = f"{remote}/"
    refs = run(
        ["for-each-ref", "--format=%(refname:short)", f"refs/remotes/{prefix}{BRANCH_PREFIX}"]
    )
    found = []
    for ref in refs.split():
        branch = ref[len(prefix) :]
        tag = release_tag(branch)
        if tag is None:
            continue
        log = run(["log", "--first-parent", "--format=%H%x09%s", f"{tag}..{ref}"])
        for line in log.splitlines():
            sha, _, subject = line.partition("\t")
            found.append(_change(sha, subject, branch))
    return found


def patch_id(sha: str, run: Run = run_git) -> str:
    """The change's diff against its first parent, as a stable id that survives a rebase."""
    diff = run(["diff", f"{sha}^1", sha])
    out = run(["patch-id", "--stable"], diff)
    return out.split()[0] if out.split() else ""


def fork_slug(run: Run = run_git, remote: str = "origin") -> str:
    """``owner/repo`` of the clone's remote, from an ssh or https GitHub URL."""
    url = run(["remote", "get-url", remote]).strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not match:
        raise RuntimeError(f"remote {remote} is not a GitHub repository: {url}")
    return match.group(1)


def http_json(url: str) -> object:
    if not url.startswith(f"{API}/"):
        raise ValueError(f"not a GitHub API address: {url}")
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "kc-upstream"}
    )
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- the address is checked above to start with the https GitHub API root
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def first_parent(commits: dict[str, tuple[list[str], str]], branch: str) -> list[Change]:
    """Walk first parents from the one tip of ``commits`` until the walk leaves the set."""
    if not commits:
        return []
    parents = {parent for links, _ in commits.values() for parent in links}
    tips = [sha for sha in commits if sha not in parents]
    if len(tips) != 1:
        raise RuntimeError(f"{branch}: expected one tip, found {len(tips)}")
    found = []
    sha: Optional[str] = tips[0]
    while sha in commits:
        links, subject = commits[sha]
        found.append(_change(sha, subject, branch))
        sha = links[0] if links else None
    return found


def api_changes(fork: str, upstream: str, fetch: Fetch = http_json) -> list[Change]:
    """The same list as ``git_changes``, read from GitHub with no clone and no token."""
    owner = upstream.split("/")[0]
    refs = fetch(f"{API}/repos/{fork}/git/matching-refs/heads/{BRANCH_PREFIX}")
    found = []
    for ref in refs if isinstance(refs, list) else []:
        branch = str(ref["ref"])[len("refs/heads/") :]
        tag = release_tag(branch)
        if tag is None:
            continue
        span = urllib.parse.quote(f"{owner}:{tag}...{branch}", safe=":./")
        commits: dict[str, tuple[list[str], str]] = {}
        page = 1
        while True:
            data = fetch(f"{API}/repos/{fork}/compare/{span}?per_page=100&page={page}")
            batch = data.get("commits", []) if isinstance(data, dict) else []
            for commit in batch:
                subject = str(commit["commit"]["message"]).split("\n", 1)[0]
                commits[commit["sha"]] = ([p["sha"] for p in commit["parents"]], subject)
            total = data.get("total_commits", 0) if isinstance(data, dict) else 0
            if not batch or len(commits) >= total:
                break
            page += 1
        found.extend(first_parent(commits, branch))
    return found
