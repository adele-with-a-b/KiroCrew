"""``kiro_crew.testing.delete_guard.disposable``: a test's own delete stays inside a temp dir."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kiro_crew.testing.delete_guard import RefusedDelete, disposable
from kiro_crew.testing.links import make_dir_link


@pytest.fixture
def fake_real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the operator's home, holding a live-looking ``.kiro/crew``."""
    home = tmp_path / "realhome"
    (home / ".kiro" / "crew" / "work-ledger" / "conductor").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_a_path_inside_the_tests_own_temp_dir_is_returned(tmp_path: Path) -> None:
    victim = tmp_path / "unit"
    victim.mkdir()
    assert disposable(victim, tmp_path=tmp_path) == victim
    assert disposable(str(victim)) == victim


def test_the_per_test_data_home_is_disposable() -> None:
    from kiro_crew.config.paths import config_dir

    assert disposable(config_dir() / "work-ledger") == config_dir() / "work-ledger"


def test_a_delete_aimed_at_the_real_home_is_refused_and_nothing_goes(
    fake_real_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = fake_real_home / ".kiro" / "crew"
    monkeypatch.setenv("KIROCREW_HOME", str(live))
    from kiro_crew import work_ledger as wl

    # The breaking property: the resolver names the live home, so a bare
    # ``shutil.rmtree(wl.conductor_dir(...))`` here deletes the operator's board.
    assert live.resolve() in wl.conductor_dir("conductor").resolve().parents
    with pytest.raises(RefusedDelete, match="inside a live data home"):
        shutil.rmtree(disposable(wl.conductor_dir("conductor")))
    with pytest.raises(RefusedDelete):
        disposable(live)
    with pytest.raises(RefusedDelete):
        disposable(fake_real_home / ".kirocrew" / "x")
    assert (live / "work-ledger" / "conductor").is_dir()


def test_a_scratch_tree_inside_the_home_is_disposable_but_its_root_is_not(
    fake_real_home: Path,
) -> None:
    scratch = fake_real_home / ".kiro" / "crew" / "scratch"
    assert disposable(scratch / "tree-1" / "home") == scratch / "tree-1" / "home"
    with pytest.raises(RefusedDelete):
        disposable(scratch / "tree-1")
    with pytest.raises(RefusedDelete):
        disposable(scratch)


def test_a_link_out_of_the_temp_dir_is_judged_by_its_target(
    tmp_path: Path, fake_real_home: Path
) -> None:
    link = tmp_path / "looks-local"
    make_dir_link(link, fake_real_home / ".kiro" / "crew")
    with pytest.raises(RefusedDelete):
        disposable(link / "work-ledger")


def test_a_path_outside_every_temp_dir_is_refused() -> None:
    with pytest.raises(RefusedDelete, match="not inside a temp dir"):
        disposable(Path(__file__).resolve().parent / "no-such-dir")


def test_a_temp_root_that_holds_the_account_home_is_no_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TMPDIR set to the account's home (or a parent of it) must not make that home disposable."""
    from kiro_crew.testing import delete_guard

    account = tmp_path / "accounts" / "me"
    (account / "Documents").mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(delete_guard, "_account_home", lambda: account.resolve())
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(account))
    monkeypatch.setattr("tempfile.tempdir", str(account))
    with pytest.raises(RefusedDelete, match="not inside a temp dir"):
        disposable(account / "Documents", tmp_path=account.parent)
    assert disposable(other / "x", tmp_path=other) == other / "x"
