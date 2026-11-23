"""``scripts/fork/kc_upstream``: a fix reaches a batch only when every check holds, and a
yes counts only for the exact texts and diff that were shown.

Git and the original's form check are replaced by fakes; the clock is injected.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts" / "fork"))

from kc_upstream import batch, cli, ledger, page, prepare  # noqa: E402

A, B = ("a" * 40, "b" * 40)
OWNER = "someone"
PR_BODY = "## Tests\n\nran them\n\n## Related Issues\n\nCloses {{issue}}\n"


@pytest.fixture(autouse=True)
def _state(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KC_UPSTREAM_STATE", str(tmp_path / "state"))


class Repo:
    """What the fake git answers for one fix's checkout."""

    def __init__(self) -> None:
        self.head = "1" * 40
        self.diff = "+++ b/x.py\n+value = 1\n"
        self.ahead, self.behind = 1, 0
        self.message = "fix: a thing\n"
        self.conflicts = False


def fake_git(repos: dict[str, Repo]):
    def run(args, stdin=None):
        if args[0] == "remote":
            return f"git@github.com:{OWNER}/KiroCrew.git\n"
        assert args[0] == "-C", args
        repo, rest = repos[args[1]], args[2:]
        if rest[0] == "rev-parse":
            return repo.head + "\n"
        if rest[0] == "rev-list":
            return f"{repo.ahead if rest[2].startswith('upstream/main..') else repo.behind}\n"
        if rest[:2] == ["diff", "--name-only"]:
            return "x.py\ny.py\n"
        if rest[0] == "diff":
            return repo.diff
        if rest[0] == "patch-id":
            return f"pid-{stdin.split()[-1]} 0000\n"
        if rest[0] == "log":
            return repo.message
        if rest[0] == "merge-tree":
            if repo.conflicts:
                raise RuntimeError("git merge-tree: CONFLICT")
            return "tree\n"
        raise AssertionError(args)

    return run


def passing_form(worktree, body):
    assert "{{issue}}" not in body and "Closes #1" in body
    return None


def write_item(slug: str, *, pr_body: str = PR_BODY, issue: str = "it breaks\n") -> Path:
    folder = prepare.item_dir(slug)
    folder.mkdir(parents=True)
    worktree = folder / "checkout"
    meta = {
        "title": f"{slug} works",
        "wrong": "It breaks today.",
        "who": "Everyone.",
        "worktree": str(worktree),
        "changes": [A[:10]],
    }
    (folder / "item.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "issue.md").write_text(f"# {slug} is broken\n\n{issue}", encoding="utf-8")
    (folder / "pr.md").write_text(f"# fix: {slug}\n\n{pr_body}", encoding="utf-8")
    return worktree


def prove(slug: str, repo: Repo, result: str = "ok") -> None:
    for name in prepare.EVIDENCE:
        prepare.record_evidence(slug, name, result, "ran it, saw it", repo.head, now=lambda: 1.0)


def judged(verdict: str = "UPSTREAM") -> None:
    ledger.append(ledger.verdicts_path(), {"sha": A, "verdict": verdict, "why": "a bug"})


def ready_item(slug: str, repos: dict[str, Repo]) -> Repo:
    repo = repos[str(write_item(slug))] = Repo()
    prove(slug, repo)
    return repo


def checks_of(slug: str, repos, markers=(OWNER,)) -> dict[str, prepare.Check]:
    found = prepare.item_checks(prepare.load(slug), list(markers), fake_git(repos), passing_form)
    return {check.name: check for check in found}


def test_an_item_with_everything_shown_passes_every_check():
    repos: dict[str, Repo] = {}
    ready_item("one", repos)
    checks = checks_of("one", repos)
    assert list(checks) == [
        "still-broken",
        "branch",
        "names",
        "tests",
        "form",
        "real-machine",
        "fails-without-fix",
    ]
    assert all(check.ok for check in checks.values())


def test_evidence_counts_only_for_the_commit_it_was_run_on():
    repos: dict[str, Repo] = {}
    repo = ready_item("one", repos)
    repo.head = "2" * 40
    checks = checks_of("one", repos)
    assert not checks["tests"].ok and "older commit" in checks["tests"].detail
    assert checks["branch"].ok


def test_missing_and_failed_evidence_are_not_ready_and_a_note_is():
    repos: dict[str, Repo] = {}
    repo = repos[str(write_item("one"))] = Repo()
    assert not checks_of("one", repos)["real-machine"].ok
    prove("one", repo, "fail")
    assert not checks_of("one", repos)["real-machine"].ok
    prove("one", repo, "note")
    check = checks_of("one", repos)["real-machine"]
    assert check.ok and check.note
    with pytest.raises(ValueError):
        prepare.record_evidence("one", "tests", "ok", " ", repo.head)
    with pytest.raises(ValueError):
        prepare.record_evidence("one", "vibes", "ok", "x", repo.head)


def test_the_branch_is_one_commit_that_applies_on_the_originals_main():
    repos: dict[str, Repo] = {}
    repo = ready_item("one", repos)
    repo.ahead = 2
    assert not checks_of("one", repos)["branch"].ok
    repo.ahead, repo.behind = 1, 3
    check = checks_of("one", repos)["branch"]
    assert check.ok and "3 commits newer" in check.detail
    repo.conflicts = True
    check = checks_of("one", repos)["branch"]
    assert not check.ok and "rebase" in check.detail


@pytest.mark.parametrize("where", ["diff", "message", "issue", "pr"])
def test_a_personal_name_or_path_anywhere_fails_the_names_check(where):
    repos: dict[str, Repo] = {}
    leak = "see /Users/pat/notes" if where == "issue" else "thanks SomeOne"
    worktree = write_item(
        "one",
        issue=f"{leak}\n" if where == "issue" else "it breaks\n",
        pr_body=PR_BODY + (f"\n{leak}\n" if where == "pr" else ""),
    )
    repo = repos[str(worktree)] = Repo()
    if where == "diff":
        repo.diff += f"+# {leak}\n"
    if where == "message":
        repo.message += leak
    check = checks_of("one", repos)["names"]
    assert not check.ok
    assert ("/Users/pat" if where == "issue" else OWNER) in check.detail


def test_a_removed_line_and_a_listed_marker_are_read_correctly(tmp_path):
    repos: dict[str, Repo] = {}
    repo = ready_item("one", repos)
    repo.diff += "-# thanks someone\n"
    assert checks_of("one", repos)["names"].ok
    (ledger.state_dir() / "markers.txt").write_text(
        "# private words\nvalue = 1\n", encoding="utf-8"
    )
    assert prepare.personal_markers(OWNER) == [OWNER, "value = 1"]
    assert not checks_of("one", repos, prepare.personal_markers(OWNER))["names"].ok


@pytest.mark.parametrize(
    "pr_body",
    [
        "## Tests\n\n<to fill when run>\n\nCloses {{issue}}\n",
        "## Tests\n\nran\n",
        "## Tests\n\nCloses {{issue}}\n\nCloses {{issue}}\n",
        "## Tests\n\nCloses {{issue}}\n\nsee {{other}}\n",
    ],
)
def test_an_unfilled_part_or_a_wrong_issue_slot_fails_the_form_check(pr_body):
    repos: dict[str, Repo] = {}
    repos[str(write_item("one", pr_body=pr_body))] = Repo()
    found = prepare.item_checks(prepare.load("one"), [], fake_git(repos), lambda w, b: None)
    assert not {check.name: check for check in found}["form"].ok


def test_the_issue_slot_belongs_to_the_pull_request_only():
    repos: dict[str, Repo] = {}
    repos[str(write_item("one", issue="it breaks\n\nCloses {{issue}}\n"))] = Repo()
    found = prepare.item_checks(prepare.load("one"), [], fake_git(repos), passing_form)
    form = {check.name: check for check in found}["form"]
    assert not form.ok and "only the pull request text may" in form.detail


def test_the_originals_own_form_check_can_refuse():
    repos: dict[str, Repo] = {}
    ready_item("one", repos)
    found = prepare.item_checks(
        prepare.load("one"), [], fake_git(repos), lambda w, b: "missing: ## Why it matters"
    )
    form = {check.name: check for check in found}["form"]
    assert not form.ok and "Why it matters" in form.detail


def test_an_item_text_needs_a_title_line():
    write_item("one")
    (prepare.item_dir("one") / "issue.md").write_text("no title\n", encoding="utf-8")
    with pytest.raises(ValueError, match="first line"):
        prepare.load("one")
    with pytest.raises(ValueError):
        prepare.item_dir("../etc")


def make(repos, names=()):
    return batch.make(list(names), OWNER, run=fake_git(repos), form=passing_form, now=lambda: 5.0)


def test_a_batch_takes_only_ready_judged_free_items():
    repos: dict[str, Repo] = {}
    ready_item("one", repos)
    with pytest.raises(ValueError, match="needs a verdict"):
        make(repos)
    judged("FORK-ONLY")
    with pytest.raises(ValueError, match="needs a verdict"):
        make(repos)
    judged()
    repos[str(write_item("two"))] = Repo()
    with pytest.raises(ValueError, match="two: not ready"):
        make(repos, ["one", "two"])
    made = make(repos)
    assert (made["batch"], [e["slug"] for e in made["items"]]) == (1, ["one"])
    assert made["account"] == OWNER and made["items"][0]["files"] == 2
    with pytest.raises(ValueError, match="waiting for an answer"):
        make(repos, ["one"])
    with pytest.raises(ValueError, match="no item is ready and free"):
        make(repos)


def test_the_code_follows_every_text_and_the_diff():
    repos: dict[str, Repo] = {}
    repo = ready_item("one", repos)
    judged()
    made = make(repos)
    run = fake_git(repos)
    assert batch.current_code(1, run) == made["code"]
    assert len(made["code"]) == 4

    pull = batch.batch_dir(1) / "1-pr.md"
    kept = pull.read_text(encoding="utf-8")
    pull.write_text(kept + "one more line\n", encoding="utf-8")
    assert batch.current_code(1, run) != made["code"]
    pull.write_text(kept, encoding="utf-8")

    repo.head = "3" * 40  # a plain rebase: a new commit, the same diff
    assert batch.current_code(1, run) == made["code"]
    repo.diff += "+more = 2\n"
    assert batch.current_code(1, run) != made["code"]
    with pytest.raises(ValueError, match="changed after it was shown"):
        batch.answer(1, f"send batch 1 {made['code']}", run=run)
    assert batch.answer_of(1) is None


def test_reply_and_read_answer_agree_on_every_choice():
    for send in ([1, 2, 3], [1, 2], [2], [1, 3], []):
        words = batch.reply(4, "ab12", send, 3)
        assert batch.read_answer(words, 4, "ab12", 3) == send
    assert batch.reply(4, "ab12", [1, 2], 3) == "send batch 4 ab12, only 1 and 2"
    assert batch.read_answer(" Send batch 4 AB12, only 1, 2 and 3. ", 4, "ab12", 3) == [1, 2, 3]


@pytest.mark.parametrize(
    "words",
    [
        "yes",
        "send batch 4",
        "send batch 4 ab12 please",
        "send batch 5 ab12",
        "send batch 4 ffff",
        "send batch 4 ab12, only 4",
        "send batch 4 ab12, only 0",
        "send all",
    ],
)
def test_words_that_are_not_an_exact_answer_are_refused(words):
    with pytest.raises(ValueError):
        batch.read_answer(words, 4, "ab12", 3)


def test_an_answer_is_recorded_once_and_a_kept_fix_can_be_asked_again():
    repos: dict[str, Repo] = {}
    ready_item("one", repos)
    ready_item("two", repos)
    judged()
    made = make(repos)
    run = fake_git(repos)
    with pytest.raises(ValueError, match="first answer"):
        batch.ask_again(1, 2)
    row = batch.answer(1, f"send batch 1 {made['code']}, only 1", run=run, now=lambda: 9.0)
    assert (row["send"], row["words"]) == ([1], f"send batch 1 {made['code']}, only 1")
    with pytest.raises(ValueError, match="already has an answer"):
        batch.answer(1, "send none", run=run)

    assert batch.held_slugs() == {
        "one": "batch 1 has a yes for it",
        "two": "kept back in batch 1; `ask-again 1 2` frees it",
    }
    with pytest.raises(ValueError, match="already has a yes"):
        batch.ask_again(1, 1)
    assert batch.ask_again(1, 2, now=lambda: 10.0) == "two"
    with pytest.raises(ValueError, match="already free"):
        batch.ask_again(1, 2)
    second = make(repos)
    assert (second["batch"], [e["slug"] for e in second["items"]]) == (2, ["two"])


def test_the_waiting_page_has_a_tick_per_fix_and_the_reply_to_type():
    repos: dict[str, Repo] = {}
    ready_item("one", repos)
    ready_item("two", repos)
    judged()
    made = make(repos)
    html = page.render(1)
    assert 'data-state="waiting"' in html
    assert html.count("data-inc=") == 2 and 'data-inc="2"' in html
    assert f'data-base="send batch 1 {made["code"]}"' in html
    assert "Send these 2 fixes to the original Kiro Crew?" in html
    assert "2 issues and 2 pull requests on kirodotdev/KiroCrew under <b>someone</b>" in html
    assert "Closes {{issue}}" in html and "one is broken" in html
    assert html.count("Tried on a real machine") == 2


def test_the_page_escapes_the_texts_it_shows():
    repos: dict[str, Repo] = {}
    repos_item = write_item("one", issue="<script>alert(1)</script>\n")
    repo = repos[str(repos_item)] = Repo()
    prove("one", repo)
    judged()
    make(repos)
    html = page.render(1)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_the_page_follows_the_answer():
    repos: dict[str, Repo] = {}
    ready_item("one", repos)
    ready_item("two", repos)
    judged()
    made = make(repos)
    run = fake_git(repos)
    batch.answer(1, f"send batch 1 {made['code']}, only 2", run=run)
    html = page.render(1)
    assert 'data-state="answered"' in html and "data-inc=" not in html
    assert "1 of 2 fixes will go to the original Kiro Crew" in html
    assert "nothing sent yet" in html and "ask again about fix 1" in html
    batch.ask_again(1, 1)
    html = page.render(1)
    assert "ask again about fix 1" not in html and "Asked again later" in html


def test_send_none_keeps_everything_and_says_so():
    repos: dict[str, Repo] = {}
    ready_item("one", repos)
    judged()
    make(repos)
    batch.answer(1, "send none", run=fake_git(repos))
    html = page.render(1)
    assert "This fix stays in your fork" in html and "Kept in your fork" in html


def test_the_commands_run_end_to_end(capsys, _floor_monkeypatch):
    repos: dict[str, Repo] = {}
    repo = repos[str(write_item("one"))] = Repo()
    judged()
    run = fake_git(repos)
    _floor_monkeypatch.setattr(prepare, "run_form", passing_form)

    assert cli.main(["item", "one"], run) == 1
    assert "not ready" in capsys.readouterr().out
    for name in prepare.EVIDENCE:
        assert cli.main(["evidence", "one", name, "ok", "--detail", "ran it"], run) == 0
    assert repo.head[:10] in capsys.readouterr().out

    cli.main(["status"], run)
    assert "1 ready for the next batch" in capsys.readouterr().out
    assert cli.main(["batch"], run) == 0
    out = capsys.readouterr().out
    code = batch.manifest(1)["code"]
    assert f"reply: send batch 1 {code}" in out
    assert (batch.batch_dir(1) / "look.html").exists()
    cli.main(["status"], run)
    assert "batch 1 waiting for a yes" in capsys.readouterr().out

    with pytest.raises(SystemExit, match="not an answer"):
        cli.main(["answer", "1", "--words", "ok go"], run)
    assert cli.main(["answer", "1", "--words", f"send batch 1 {code}"], run) == 0
    assert "Nothing is sent." in capsys.readouterr().out
    assert 'data-state="answered"' in (batch.batch_dir(1) / "look.html").read_text(encoding="utf-8")
    cli.main(["status"], run)
    assert "batch 1 has a yes, not sent yet" in capsys.readouterr().out
