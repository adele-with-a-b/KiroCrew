"""``scripts/fork/kc_upstream`` send door: it files only what a recorded yes covers, only
while the batch is still what was shown, and never the same thing twice.

Git and GitHub are replaced by fakes; the clock is injected. Nothing here reaches a network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "fork"))

from kc_upstream import batch, cli, journal, ledger, page, prepare, send  # noqa: E402
from test_fork_kc_upstream_prepare import (  # noqa: E402
    OWNER,
    Repo,
    fake_git,
    judged,
    make,
    passing_form,
    ready_item,
)

TARGET = "kirodotdev/KiroCrew"
YES_AT = 10_000.0
BEFORE_YES = "1970-01-01T00:00:00Z"
AFTER_YES = "1970-01-01T03:00:00Z"


@pytest.fixture(autouse=True)
def _state(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KC_UPSTREAM_STATE", str(tmp_path / "state"))
    _floor_monkeypatch.delenv("KC_UPSTREAM_REPO", raising=False)


def git_for(repos: dict[str, Repo], pushes: list[str]):
    inner = fake_git(repos)

    def run(args, stdin=None):
        if args[0] == "-C" and args[2] == "remote":
            return inner(args[2:], stdin)
        if args[0] == "-C" and args[2:4] == ["rev-parse", "--abbrev-ref"]:
            return "up/" + args[1].rstrip("/").split("/")[-2] + "\n"
        if args[0] == "-C" and args[2:4] == ["log", "-1"] and "%an" in args[4]:
            return "Some One <one@example.com>\nA Bot <bot@example.com>\n"
        if args[0] == "-C" and args[2] == "push":
            pushes.append(" ".join(args[3:]))
            return ""
        return inner(args, stdin)

    return run


class Hub:
    """A GitHub that remembers what was filed, and can fail on one command."""

    def __init__(self) -> None:
        self.login = OWNER
        self.issues: list[dict] = []
        self.pulls: list[dict] = []
        self.calls: list[str] = []
        self.fail: str = ""
        self.lose_reply = False

    def _url(self, repo: str, kind: str) -> str:
        return f"https://github.com/{repo}/{kind}/{len(self.issues) + len(self.pulls) + 1}"

    def __call__(self, args, stdin=None):
        name = " ".join(args[:2])
        self.calls.append(name)
        failing = name == self.fail
        if failing and not self.lose_reply:
            raise RuntimeError(f"gh {name}: HTTP 502")
        if name == "api user":
            return self.login + "\n"
        if args[0] == "api" and "/issues?" in args[1]:
            return json.dumps(self.issues)
        if args[0] == "api" and "/pulls?" in args[1]:
            head = args[1].split("head=")[1].split("&")[0]
            return json.dumps([pull for pull in self.pulls if pull["head"] == head])
        if name == "issue edit":
            found = next(i for i in self.issues if i["html_url"].endswith(f"/issues/{args[2]}"))
            option = dict(zip(args[3::2], args[4::2]))
            found.update(title=option["--title"], body=stdin)
            return ""
        option = dict(zip(args[2::2], args[3::2]))
        assert option["--body-file"] == "-" and "{{issue}}" not in stdin
        if name == "issue create":
            url = self._url(option["--repo"], "issues")
            found = {"title": option["--title"], "body": stdin, "html_url": url}
            self.issues.append({**found, "created_at": AFTER_YES, "repo": option["--repo"]})
        else:
            assert name == "pr create" and option["--base"] == "main"
            url = self._url(option["--repo"], "pull")
            found = {"title": option["--title"], "body": stdin, "html_url": url}
            self.pulls.append({**found, "head": option["--head"], "repo": option["--repo"]})
        if failing:
            raise RuntimeError(f"gh {name}: connection reset")
        return url + "\n"

    def writes(self) -> list[str]:
        return [call for call in self.calls if not call.startswith("api")]


class World:
    def __init__(self, count: int = 2) -> None:
        self.repos: dict[str, Repo] = {}
        self.pushes: list[str] = []
        self.hub = Hub()
        self.said: list[str] = []
        self.slugs = ["one", "two", "zed"][:count]
        for slug in self.slugs:
            ready_item(slug, self.repos)
        judged()
        self.run = git_for(self.repos, self.pushes)
        self.made = make(self.repos)
        self.code = self.made["code"]

    def repo(self, slug: str) -> Repo:
        return self.repos[str(prepare.item_dir(slug) / "checkout")]

    def yes(self, only: str = "") -> None:
        words = "send none" if only == "none" else f"send batch 1 {self.code}{only}"
        batch.answer(1, words, run=self.run, now=lambda: YES_AT)

    def send(self, number: int = 1, **more) -> dict:
        return send.send(
            number, run=self.run, gh=self.hub, now=lambda: YES_AT + 60, say=self.said.append, **more
        )


def test_nothing_is_sent_without_a_yes_that_names_the_batch_code():
    world = World()
    with pytest.raises(ValueError, match="has no answer"):
        world.send()
    world.yes("none")
    with pytest.raises(ValueError, match="send none"):
        world.send()
    assert world.hub.calls == [] and world.pushes == []


def test_a_yes_recorded_for_another_code_sends_nothing():
    world = World()
    row = {"kind": "answer", "batch": 1, "code": "0000", "send": [1, 2], "words": "x", "at": 1.0}
    batch._append(row)
    with pytest.raises(ValueError, match="another code"):
        world.send()
    assert world.hub.calls == []


def test_the_door_files_as_the_account_the_batch_was_made_for():
    world = World()
    world.yes()
    world.hub.login = "somebody-else"
    with pytest.raises(ValueError, match="signed in as 'somebody-else'"):
        world.send()
    assert world.hub.writes() == []


def test_a_full_yes_files_issue_then_branch_then_pull_request_per_fix():
    world = World()
    world.yes()
    assert world.send() == {"sent": [1, 2], "approved": [1, 2]}
    assert world.hub.writes() == ["issue create", "pr create", "issue create", "pr create"]
    assert world.pushes == [
        f"--force origin {'1' * 40}:refs/heads/up/one",
        f"--force origin {'1' * 40}:refs/heads/up/two",
    ]
    first, second = world.hub.pulls
    assert (first["head"], first["repo"]) == (f"{OWNER}:up/one", TARGET)
    assert "Closes #1\n" in first["body"] and "Closes #3\n" in second["body"]
    assert world.hub.issues[0]["title"] == "one is broken"
    steps = [(row["n"], row["step"]) for row in journal.rows(1)]
    assert steps == [(1, "push"), (1, "issue"), (1, "pr"), (2, "push"), (2, "issue"), (2, "pr")]
    assert journal.step_of(journal.rows(1), 2, "pr")["url"].endswith("/pull/4")

    world.send()
    assert len(world.hub.issues) == 2 and len(world.hub.pulls) == 2 and len(world.pushes) == 2
    assert batch.held_slugs() == {"one": "sent in batch 1", "two": "sent in batch 1"}


def test_an_untick_sends_exactly_the_fixes_that_were_named():
    world = World(3)
    world.yes(", only 1 and 3")
    assert world.send()["sent"] == [1, 3]
    assert [issue["title"] for issue in world.hub.issues] == ["one is broken", "zed is broken"]
    assert [pull["head"] for pull in world.hub.pulls] == [f"{OWNER}:up/one", f"{OWNER}:up/zed"]
    assert any("fix 2 (two): kept back" in line for line in world.said)
    html = page.render(1)
    assert 'data-state="sent"' in html and "2 sent · 1 kept back" in html
    assert "Fixes 1 and 3 are on the original" in html
    assert html.count('class="ghlink"') == 4 and "ask again about fix 2" in html
    assert html.count('class="s-on"') == 2 and html.count('class="s-off"') == 1


def _edit_item(world: World) -> None:
    path = prepare.item_dir("two") / "pr.md"
    path.write_text(path.read_text(encoding="utf-8") + "one more word\n", encoding="utf-8")


def _edit_saved_copy(world: World) -> None:
    path = batch.batch_dir(1) / "2-issue.md"
    path.write_text(path.read_text(encoding="utf-8") + "one more word\n", encoding="utf-8")
    item = prepare.item_dir("two") / "issue.md"
    item.write_text(item.read_text(encoding="utf-8") + "one more word\n", encoding="utf-8")


def _change_code(world: World) -> None:
    world.repo("two").diff += "+more = 2\n"


def _new_commit(world: World) -> None:
    world.repo("two").head = "9" * 40


def _branch_name_marker(world: World) -> None:
    (ledger.state_dir() / "markers.txt").write_text("up/two\n", encoding="utf-8")


def _new_marker(world: World) -> None:
    (ledger.state_dir() / "markers.txt").write_text("value = 1\n", encoding="utf-8")


@pytest.mark.parametrize(
    "change, why",
    [
        (_edit_item, "fix 2: the issue or pull request text was edited"),
        (_edit_saved_copy, "fix 2: its saved texts were changed"),
        (_change_code, "fix 2: the code change is different"),
        (_new_commit, "fix 2: the branch has a different commit"),
        (_new_marker, "a personal word is in it: value = 1"),
        (_branch_name_marker, "fix 2: a personal word is in it: up/two"),
    ],
)
def test_any_change_after_the_yes_stops_the_batch_until_it_is_asked_again(change, why):
    world = World()
    world.yes()
    change(world)
    with pytest.raises(send.Stopped, match=why):
        world.send()
    assert world.hub.writes() == [] and world.pushes == []
    stop = journal.stopped(journal.rows(1))
    assert stop["was"] == world.code and stop["fixes"]

    world.repos[str(prepare.item_dir("two") / "checkout")] = Repo()
    with pytest.raises(send.Stopped, match="reask 1"):
        world.send()
    assert world.hub.writes() == []
    assert 'data-state="changed"' in page.render(1)
    assert "Your yes no longer counts" in page.render(1)


def test_a_change_to_a_fix_that_is_not_being_sent_does_not_stop_the_rest():
    world = World()
    world.yes(", only 1")
    _change_code(world)
    _edit_item(world)
    assert world.send()["sent"] == [1]
    assert journal.stopped(journal.rows(1)) is None


def test_a_sent_batch_stays_sent_when_its_branches_move_on():
    world = World()
    world.yes()
    world.send()
    _change_code(world)
    _new_marker(world)
    assert world.send()["sent"] == [1, 2]
    assert journal.stopped(journal.rows(1)) is None and 'data-state="sent"' in page.render(1)


def test_a_checkout_that_cannot_be_read_sends_nothing_and_says_so():
    world = World()
    world.yes()
    del world.repos[str(prepare.item_dir("two") / "checkout")]

    def run(args, stdin=None):
        try:
            return world.run(args, stdin)
        except KeyError as error:
            raise RuntimeError(f"git: not a repository: {error}") from error

    with pytest.raises(ValueError, match="could not be checked, so nothing was sent"):
        send.send(1, run=run, gh=world.hub, say=world.said.append)
    assert world.hub.writes() == [] and not journal.path().exists()


def test_an_issue_filed_before_a_change_is_reused_by_the_next_batch():
    world = World()
    world.yes()
    world.hub.fail = "pr create"
    with pytest.raises(RuntimeError):
        world.send()
    world.hub.fail = ""
    issue = prepare.item_dir("one") / "issue.md"
    issue.write_text(issue.read_text(encoding="utf-8") + "A better account.\n", encoding="utf-8")
    with pytest.raises(send.Stopped, match="fix 1: the issue or pull request text was edited"):
        world.send()
    html = page.render(1)
    assert 'data-state="changed"' in html and ">Issue #1</a>" in html

    assert batch.reask(1) == ["one", "two"]
    second = make(world.repos)
    batch.answer(2, f"send batch 2 {second['code']}", run=world.run, now=lambda: YES_AT + 900)
    world.said.clear()
    world.send(2, dry_run=True)
    assert any("issues/1 is brought to this text" in line for line in world.said)
    assert world.send(2)["sent"] == [1, 2]
    assert [found["title"] for found in world.hub.issues] == ["one is broken", "two is broken"]
    assert world.hub.issues[0]["body"].endswith("A better account.\n")
    assert world.hub.writes().count("issue edit") == 1
    assert "Closes #1\n" in world.hub.pulls[0]["body"]
    assert journal.step_of(journal.rows(2), 1, "issue")["number"] == 1


def test_an_open_pull_request_that_is_not_this_one_is_left_alone():
    world = World(1)
    world.yes()
    stray = {"title": "something else", "body": "by hand", "head": f"{OWNER}:up/one"}
    world.hub.pulls.append({**stray, "html_url": f"https://github.com/{TARGET}/pull/90"})
    with pytest.raises(ValueError, match="pull/90 is already open from up/one and is not this"):
        world.send()
    assert world.hub.writes() == [] and world.pushes == [] and not journal.path().exists()
    world.hub.pulls.clear()
    assert world.send()["sent"] == [1]
    assert len(world.hub.issues) == 1


def test_only_a_fix_branch_is_ever_pushed():
    world = World(1)
    world.yes()
    inner = world.run

    def run(args, stdin=None):
        if args[2:4] == ["rev-parse", "--abbrev-ref"]:
            return "main\n"
        return inner(args, stdin)

    with pytest.raises(ValueError, match="branch 'main' of someone/KiroCrew cannot be sent"):
        send.send(1, run=run, gh=world.hub, say=world.said.append)
    assert world.hub.writes() == [] and world.pushes == []


def test_two_fixes_on_one_branch_send_nothing():
    world = World()
    world.yes()
    inner = world.run

    def run(args, stdin=None):
        if args[2:4] == ["rev-parse", "--abbrev-ref"]:
            return "up/one\n"
        return inner(args, stdin)

    with pytest.raises(ValueError, match="two fixes are on one branch"):
        send.send(1, run=run, gh=world.hub, say=world.said.append)
    assert world.hub.writes() == [] and world.pushes == []


def test_an_issue_on_another_repo_is_never_reused():
    world = World(1)
    row = {"n": 1, "slug": "one", "url": "https://github.com/someone/elsewhere/issues/7"}
    journal.record(9, "issue", YES_AT, number=7, **row)
    assert journal.issue_awaiting_pull("one", 1, world.made["upstream"]) is None
    assert journal.issue_awaiting_pull("one", 1, "someone/elsewhere")["number"] == 7


def test_two_fixes_with_one_issue_title_get_an_issue_each():
    world = World()
    path = prepare.item_dir("two") / "issue.md"
    path.write_text("# one is broken\n\nit breaks too\n", encoding="utf-8")
    assert batch.reask(1) == ["one", "two"]
    second = make(world.repos)
    batch.answer(2, f"send batch 2 {second['code']}", run=world.run, now=lambda: YES_AT)
    world.send(2)
    assert len(world.hub.issues) == 2
    assert (
        "Closes #1\n" in world.hub.pulls[0]["body"] and "Closes #3\n" in world.hub.pulls[1]["body"]
    )


def test_asking_again_makes_a_new_batch_with_a_new_code():
    world = World()
    world.yes()
    _edit_item(world)
    with pytest.raises(send.Stopped):
        world.send()
    html = page.render(1)
    assert "Fix 2 changed after you said yes, so I stopped" in html
    assert "+one more word" in html and "not changed, not sent" in html

    assert batch.reask(1, now=lambda: YES_AT + 90) == ["one", "two"]
    with pytest.raises(ValueError, match="already replaced"):
        batch.reask(1)
    with pytest.raises(ValueError, match="replaced by a newer question"):
        world.send()
    second = make(world.repos)
    assert second["batch"] == 2 and second["code"] != world.code
    assert "A newer batch asks again." in page.render(1)
    with pytest.raises(ValueError, match="has no answer"):
        world.send(2)
    batch.answer(2, f"send batch 2 {second['code']}", run=world.run, now=lambda: YES_AT + 100)
    assert world.send(2)["sent"] == [1, 2]
    assert "Closes #1\n" in world.hub.pulls[0]["body"]
    assert "one more word" in world.hub.pulls[1]["body"]


def test_a_waiting_batch_can_be_replaced_and_then_takes_no_answer():
    world = World()
    assert batch.reask(1) == ["one", "two"]
    with pytest.raises(ValueError, match="answer the newer batch"):
        world.yes()
    html = page.render(1)
    assert 'data-state="replaced"' in html and "data-inc=" not in html
    assert make(world.repos)["batch"] == 2


def test_asking_again_keeps_what_was_sent_and_what_was_kept_back():
    world = World(3)
    world.yes(", only 1 and 3")
    world.hub.fail = "pr create"
    with pytest.raises(RuntimeError):
        world.send()
    world.hub.fail = ""
    world.send()
    with pytest.raises(ValueError, match="nothing left"):
        batch.reask(1)
    assert batch.held_slugs()["two"].startswith("kept back in batch 1")


def test_a_batch_replaced_partway_says_where_the_rest_went():
    world = World()
    world.yes()
    world.hub.fail = "pr create"
    with pytest.raises(RuntimeError):
        world.send()
    assert batch.reask(1) == ["one", "two"]
    html = page.render(1)
    assert "Replaced by a newer question" in html and "carries on from here" not in html
    assert ">Issue #1</a>" in html


def test_the_waiting_page_shows_who_signed_the_commit():
    world = World()
    batch.reask(1)
    batch.make([], OWNER, run=world.run, form=passing_form, now=lambda: 5.0)
    signed = "Some One &lt;one@example.com&gt;; committed by A Bot &lt;bot@example.com&gt;"
    assert f"The commit is signed:</b> {signed}" in page.render(2)
    assert batch._signed("A <a@b>\nA <a@b>\n") == "A <a@b>"


@pytest.mark.parametrize("lose_reply", [False, True])
@pytest.mark.parametrize("failing", ["issue create", "pr create"])
def test_a_run_that_stops_halfway_carries_on_without_filing_twice(failing, lose_reply):
    world = World()
    world.yes()
    world.hub.fail, world.hub.lose_reply = failing, lose_reply
    with pytest.raises(RuntimeError):
        world.send()
    filed_issue = failing == "pr create" or lose_reply
    assert len(world.hub.issues) == int(filed_issue)
    recorded = [row["step"] for row in journal.rows(1)]
    assert recorded == (["push", "issue"] if failing == "pr create" else ["push"])
    if failing == "pr create":
        html = page.render(1)
        assert "Sending stopped partway" in html and "Issue filed · pull request not yet" in html
        assert "0 of 2 fixes on the original so far" in html

    world.hub.fail = ""
    assert world.send()["sent"] == [1, 2]
    assert [issue["title"] for issue in world.hub.issues] == ["one is broken", "two is broken"]
    assert [pull["head"] for pull in world.hub.pulls] == [f"{OWNER}:up/one", f"{OWNER}:up/two"]
    wanted = world.hub.issues[0]["html_url"].rsplit("/", 1)[1]
    assert f"Closes #{wanted}\n" in world.hub.pulls[0]["body"]
    assert [row["step"] for row in journal.rows(1)].count("issue") == 2


def test_an_issue_from_before_the_yes_is_not_taken_for_this_one():
    world = World(1)
    world.yes()
    old = {"title": "one is broken", "html_url": f"https://github.com/{TARGET}/issues/1"}
    world.hub.issues.append({**old, "created_at": BEFORE_YES})
    world.hub.issues.append({"title": "one is broken", "pull_request": {}, "created_at": AFTER_YES})
    world.send()
    assert world.hub.writes() == ["issue create", "pr create"]


def test_a_dry_run_prints_everything_in_order_and_writes_nothing():
    world = World()
    world.send(dry_run=True)
    assert "no answer yet" in world.said[0]
    world.said.clear()
    world.yes(", only 2")
    world.send(dry_run=True)
    out = "\n".join(world.said)
    assert "dry run: nothing is filed" in out and "fix 1 (one): kept back, not sent" in out
    places = [out.index(mark) for mark in ("1. PUSH", "2. ISSUE on", "3. PULL REQUEST on")]
    assert places == sorted(places)
    assert "title: two is broken" in out and "title: fix: two" in out
    assert f"Closes {send.ISSUE_NUMBER_PENDING}" in out and "{{issue}}" not in out
    assert world.hub.writes() == [] and world.pushes == []
    assert not journal.path().exists()

    _edit_item(world)
    with pytest.raises(send.Stopped):
        world.send(dry_run=True)
    assert not journal.path().exists()


def test_a_rehearsal_batch_files_on_the_repository_it_was_made_for(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KC_UPSTREAM_REPO", f"{OWNER}/scratch")
    world = World(1)
    assert world.made["upstream"] == f"{OWNER}/scratch"
    _floor_monkeypatch.delenv("KC_UPSTREAM_REPO")
    world.yes()
    world.send()
    assert {found["repo"] for found in world.hub.issues + world.hub.pulls} == {f"{OWNER}/scratch"}


def test_the_sent_page_is_drawn_from_the_record():
    world = World()
    world.yes()
    world.send()
    html = page.render(1)
    assert 'data-state="sent"' in html and "Both are on the original" in html
    assert f"2 issues and 2 pull requests, filed as {OWNER} on " in html
    assert f'href="https://github.com/{TARGET}/issues/1">Issue #1</a>' in html
    assert f'href="https://github.com/{TARGET}/pull/4">Pull request #4</a>' in html
    assert "data-inc=" not in html


def test_the_commands_send_and_ask_again(capsys, _floor_monkeypatch):
    world = World()
    _floor_monkeypatch.setattr(prepare, "run_form", passing_form)
    _floor_monkeypatch.setattr(send, "run_gh", world.hub)
    with pytest.raises(SystemExit, match="has no answer"):
        cli.main(["send", "1"], world.run)
    assert cli.main(["send", "1", "--dry-run"], world.run) == 0
    assert "2. ISSUE on" in capsys.readouterr().out
    world.yes()
    cli.main(["status"], world.run)
    assert "batch 1 has a yes, not sent yet" in capsys.readouterr().out

    world.hub.fail = "pr create"
    with pytest.raises(SystemExit, match="stopped partway.*`send 1` carries on"):
        cli.main(["send", "1"], world.run)
    world.hub.fail = ""
    assert cli.main(["send", "1"], world.run) == 0
    assert "batch 1: 2 sent, 0 left" in capsys.readouterr().out
    assert 'data-state="sent"' in (batch.batch_dir(1) / "look.html").read_text(encoding="utf-8")
    cli.main(["status"], world.run)
    assert "batch 1" not in capsys.readouterr().out


def test_the_commands_stop_and_replace_a_changed_batch(capsys, _floor_monkeypatch):
    world = World()
    _floor_monkeypatch.setattr(prepare, "run_form", passing_form)
    _floor_monkeypatch.setattr(send, "run_gh", world.hub)
    world.yes()
    _edit_item(world)
    with pytest.raises(SystemExit, match="not what was shown"):
        cli.main(["send", "1"], world.run)
    cli.main(["status"], world.run)
    assert "batch 1 changed after the yes and needs a new question" in capsys.readouterr().out
    assert cli.main(["reask", "1"], world.run) == 0
    assert "free for the next batch: one, two" in capsys.readouterr().out
    cli.main(["status"], world.run)
    assert "2 ready for the next batch" in capsys.readouterr().out
