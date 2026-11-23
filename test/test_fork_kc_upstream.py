"""``scripts/fork/kc_upstream``: no fork change is ever without an upstream verdict.

Git and GitHub are replaced by fakes: the tests pin which commits count as fork
changes, that an unjudged one is always reported, and that a damaged record reads
as an error instead of as "judged".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts" / "fork"))

from kc_upstream import UPSTREAM, cli, ledger, watch  # noqa: E402
from kc_upstream.changes import (  # noqa: E402
    Change,
    api_changes,
    first_parent,
    fork_slug,
    git_changes,
    http_json,
    release_tag,
)

A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
FORK = "someone/KiroCrew"


@pytest.fixture(autouse=True)
def _state(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KC_UPSTREAM_STATE", str(tmp_path / "state"))


def fake_git(log: str, diffs: dict[str, str] | None = None):
    """A ``run`` that answers the git calls the CLI makes on one ``local/1.2.3`` branch."""

    def run(args, stdin=None):
        if args[0] == "for-each-ref":
            return "origin/local/1.2.3\norigin/local/scratch\n"
        if args[0] == "log":
            assert args[-1] == "v1.2.3..origin/local/1.2.3"
            return log
        if args[0] == "diff":
            return (diffs or {}).get(args[2], f"diff of {args[2]}")
        if args[0] == "patch-id":
            return f"pid-{stdin.replace(' ', '_')} 0000\n"
        if args[0] == "remote":
            return "git@github.com:someone/KiroCrew.git\n"
        raise AssertionError(args)

    return run


LOG = f"{C}\tMerge pull request #7 from someone/topic\n{B}\tfix: a direct push\n"


def test_release_tag_only_for_a_release_branch():
    assert release_tag("local/0.13.0") == "v0.13.0"
    assert release_tag("local/scratch") is None
    assert release_tag("main") is None


def test_git_changes_are_first_parent_commits_after_the_release_tag():
    assert git_changes(fake_git(LOG)) == [
        Change(C, "Merge pull request #7 from someone/topic", "local/1.2.3", 7),
        Change(B, "fix: a direct push", "local/1.2.3", None),
    ]


def test_fork_slug_reads_ssh_and_https_remotes():
    assert fork_slug(fake_git("")) == FORK
    assert fork_slug(lambda args, stdin=None: "https://github.com/someone/KiroCrew\n") == FORK
    with pytest.raises(RuntimeError):
        fork_slug(lambda args, stdin=None: "/srv/git/repo\n")


def test_http_json_refuses_an_address_outside_the_github_api():
    with pytest.raises(ValueError):
        http_json("file:///etc/hosts")


def test_first_parent_skips_the_commits_a_merge_brought_in():
    commits = {
        C: ([B, D], "Merge pull request #7 from someone/topic"),
        D: ([A], "feat: the pull request's own commit"),
        B: ([A], "fix: a direct push"),
    }
    assert [change.sha for change in first_parent(commits, "local/1.2.3")] == [C, B]
    assert first_parent({}, "local/1.2.3") == []


def test_first_parent_refuses_two_tips():
    with pytest.raises(RuntimeError):
        first_parent({C: ([A], "one"), D: ([A], "two")}, "local/1.2.3")


def fake_api(pages: list[list[dict]]):
    calls = []

    def fetch(url):
        calls.append(url)
        if "/git/matching-refs/heads/local/" in url:
            return [{"ref": "refs/heads/local/1.2.3"}, {"ref": "refs/heads/local/scratch"}]
        assert f"/repos/{FORK}/compare/kirodotdev:v1.2.3...local/1.2.3?" in url
        page = int(url.rsplit("page=", 1)[1])
        total = sum(len(p) for p in pages)
        return {"total_commits": total, "commits": pages[page - 1]}

    fetch.calls = calls
    return fetch


def api_commit(sha: str, parents: list[str], message: str) -> dict:
    return {"sha": sha, "parents": [{"sha": p} for p in parents], "commit": {"message": message}}


def test_api_changes_match_the_git_reading_across_pages():
    fetch = fake_api(
        [
            [api_commit(B, [A], "fix: a direct push\n\nbody")],
            [
                api_commit(D, [A], "feat: the pull request's own commit"),
                api_commit(C, [B, D], "Merge pull request #7 from someone/topic\n\ntitle"),
            ],
        ]
    )
    assert api_changes(FORK, UPSTREAM, fetch) == git_changes(fake_git(LOG))
    assert len(fetch.calls) == 3


def test_ledger_refuses_a_bad_row_and_keeps_the_last_verdict(tmp_path):
    path = tmp_path / "v.jsonl"
    good = {"sha": A, "verdict": "UPSTREAM", "why": "a bug everyone hits"}
    for bad in (dict(good, sha="abc"), dict(good, verdict="MAYBE"), dict(good, why=" ")):
        with pytest.raises(ValueError):
            ledger.append(path, bad)
    assert not path.exists()
    ledger.append(path, good)
    ledger.append(path, dict(good, verdict="FORK-ONLY"))
    assert ledger.latest(ledger.read(path))[A]["verdict"] == "FORK-ONLY"


def test_a_damaged_ledger_is_an_error_not_a_verdict(tmp_path):
    path = tmp_path / "v.jsonl"
    path.write_text('{"sha": "x"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=":2:"):
        ledger.read(path)
    path.write_text("[1]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        ledger.read(path)


def test_pending_shrinks_only_when_a_verdict_is_recorded(capsys):
    run = fake_git(LOG)
    assert cli.main(["pending"], run) == 0
    assert "2 pending of 2 fork changes" in capsys.readouterr().out

    assert cli.main(["verdict", "bbbb", "UPSTREAM", "--why", "a bug everyone hits"], run) == 0
    row = ledger.read(ledger.verdicts_path())[0]
    assert (row["sha"], row["verdict"], row["pr"], row["strip"]) == (B, "UPSTREAM", None, "none")
    assert row["patch_id"] == f"pid-diff_of_{B}"
    capsys.readouterr()

    cli.main(["pending"], run)
    out = capsys.readouterr().out
    assert "1 pending of 2 fork changes" in out and C[:10] in out and B[:10] not in out


def test_verdict_refuses_a_commit_that_is_not_a_fork_change():
    with pytest.raises(SystemExit, match="matches 0"):
        cli.main(["verdict", "ffff", "UPSTREAM", "--why", "x"], fake_git(LOG))
    assert not ledger.verdicts_path().exists()


def test_carry_copies_a_verdict_onto_the_same_diff_at_a_new_commit(capsys):
    old_run = fake_git(f"{B}\tfix: a direct push\n", {B: "same diff"})
    cli.main(["verdict", B, "SPLIT", "--why", "generic seam", "--strip", "names"], old_run)

    moved = fake_git(f"{D}\tfix: a direct push\n{C}\tfeat: new work\n", {D: "same diff"})
    assert cli.main(["carry"], moved) == 0
    assert "1 carried" in capsys.readouterr().out
    rows = ledger.latest(ledger.read(ledger.verdicts_path()))
    assert (rows[D]["verdict"], rows[D]["strip"], rows[D]["by"]) == (
        "SPLIT",
        "names",
        f"carry of {B[:10]}",
    )
    assert C not in rows


def test_list_and_brief_name_the_unjudged_change(capsys):
    run = fake_git(LOG)
    cli.main(["verdict", B, "FORK-ONLY", "--why", "only this fork"], run)
    capsys.readouterr()
    cli.main(["list"], run)
    out = capsys.readouterr().out
    assert f"UNJUDGED         {C[:10]}" in out and f"FORK-ONLY        {B[:10]}" in out
    cli.main(["brief"], run)
    out = capsys.readouterr().out
    assert out.startswith("1 change(s)") and "(PR #7)" in out and B[:10] not in out


def test_notice_fires_once_per_set_then_again_after_a_day(tmp_path):
    seen = tmp_path / "seen.json"
    clock = [1000.0]
    fetch = fake_api([[api_commit(B, [A], "fix: a direct push")]])

    def notice():
        return watch.pending_notice(FORK, seen, fetch=fetch, now=lambda: clock[0])

    text, key = notice()
    assert key == B and f"- {B[:10]} [local/1.2.3] fix: a direct push" in text
    assert f"Write nothing to {UPSTREAM}" in text

    assert notice() is not None, "a send that was never recorded is tried again"
    watch.record_notice(seen, key, now=lambda: clock[0])
    assert notice() is None

    clock[0] += watch.REPEAT_SECONDS
    assert notice() is not None

    ledger.append(ledger.verdicts_path(), {"sha": B, "verdict": "UPSTREAM", "why": "a bug"})
    assert notice() is None


def test_notice_fires_at_once_when_the_unjudged_set_changes(tmp_path):
    seen = tmp_path / "seen.json"
    seen.write_text(json.dumps({"key": A, "at": 1000.0}), encoding="utf-8")
    fetch = fake_api([[api_commit(B, [A], "fix: a direct push")]])
    assert watch.pending_notice(FORK, seen, fetch=fetch, now=lambda: 1001.0) is not None
