"""``kiro_crew.testing.delete_guard.disposable``: a test's own delete stays inside a temp dir."""

from __future__ import annotations

import os
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

    board = wl.conductor_dir("conductor")
    (board / "items").mkdir(parents=True)
    # The breaking property: the resolver names an existing board in the live home, so a
    # bare ``shutil.rmtree(wl.conductor_dir(...))`` here deletes it.
    assert live.resolve() in board.resolve().parents
    with pytest.raises(RefusedDelete, match="inside a live data home"):
        shutil.rmtree(disposable(wl.conductor_dir("conductor")))
    with pytest.raises(RefusedDelete):
        disposable(live)
    with pytest.raises(RefusedDelete):
        disposable(fake_real_home / ".kirocrew" / "x")
    assert (board / "items").is_dir()


def test_a_scratch_tree_inside_the_home_is_disposable_but_its_root_is_not(
    fake_real_home: Path,
) -> None:
    scratch = fake_real_home / ".kiro" / "crew" / "scratch"
    assert disposable(scratch / "tree-1" / "home") == scratch / "tree-1" / "home"
    with pytest.raises(RefusedDelete):
        disposable(scratch / "tree-1")
    with pytest.raises(RefusedDelete):
        disposable(scratch)


def test_a_path_through_a_link_into_the_live_home_is_refused(
    tmp_path: Path, fake_real_home: Path
) -> None:
    link = tmp_path / "looks-local"
    make_dir_link(link, fake_real_home / ".kiro" / "crew")
    with pytest.raises(RefusedDelete, match="inside a live data home"):
        disposable(link / "work-ledger")
    # The entry is in tmp, but a delete that follows it (``rglob``, ``rm -rf link/``)
    # lands in the home, so the link's real path is judged too.
    with pytest.raises(RefusedDelete, match="inside a live data home"):
        disposable(link)


def test_a_link_entry_inside_the_live_home_is_refused_wherever_it_points(
    tmp_path: Path, fake_real_home: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    entry = fake_real_home / ".kiro" / "crew" / "work-ledger" / "to-tmp"
    make_dir_link(entry, plain)
    # The breaking property: the link's target is a plain tmp dir, but ``entry.unlink()``
    # removes an entry in the live home.
    assert entry.resolve() == plain.resolve()
    with pytest.raises(RefusedDelete, match="inside a live data home"):
        disposable(entry)
    assert entry.is_dir()


def test_a_link_from_tmp_to_outside_every_temp_dir_is_refused(tmp_path: Path) -> None:
    link = tmp_path / "to-the-root"
    make_dir_link(link, Path(Path.cwd().anchor))
    try:
        with pytest.raises(RefusedDelete, match="not inside a temp dir"):
            disposable(link / "kc-no-such-dir", tmp_path=tmp_path)
    finally:
        # A kept tmp dir (a failed run) must not hold a link to the whole disk.
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


def test_no_home_at_all_refuses_even_a_path_in_the_tests_own_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no home there is no data-home fence, so the guard fails closed."""
    from kiro_crew.testing import delete_guard

    def no_home(cls: type) -> Path:
        raise RuntimeError("Could not determine home directory.")

    def no_entry(uid: int) -> object:
        raise KeyError(uid)

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    if os.name == "nt":
        monkeypatch.delenv("USERPROFILE", raising=False)
    else:
        import pwd

        monkeypatch.setattr(pwd, "getpwuid", no_entry)
    assert delete_guard._account_home() is None
    with pytest.raises(RefusedDelete, match="no home directory could be found"):
        disposable(tmp_path / "unit", tmp_path=tmp_path)


def test_a_path_outside_every_temp_dir_is_refused() -> None:
    with pytest.raises(RefusedDelete, match="not inside a temp dir"):
        disposable(Path(Path.cwd().anchor) / "kc-no-such-dir")


def test_a_per_test_path_beside_the_runs_own_temp_root_is_disposable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor points ``tempfile`` at ``<start>/kc-pytest-*`` but keeps basetemp in ``<start>``.

    ``<start>`` here is not ``/tmp``, the case of a run whose ``TMPDIR`` is set elsewhere.
    """
    from kiro_crew.testing import delete_guard

    start = tmp_path / "start"
    run_root = start / "kc-pytest-me-1-abc"
    run_root.mkdir(parents=True)
    # An account home under tmp_path drops /tmp itself as a fence, so only the run's roots count.
    monkeypatch.setattr(delete_guard, "_account_home", lambda: (tmp_path / "acct").resolve())
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(run_root))
    monkeypatch.setattr("tempfile.tempdir", str(run_root))
    per_test_home = start / "pytest-of-me" / "pytest-0" / "i0" / "kirocrew-home"
    assert disposable(per_test_home) == per_test_home
    with pytest.raises(RefusedDelete, match="not inside a temp dir"):
        disposable(tmp_path / "beside-start")


def test_the_rootdir_conftest_names_this_runs_basetemp(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    from kiro_crew.testing import delete_guard

    assert tmp_path_factory.getbasetemp().resolve() in delete_guard._RUN_TEMP_TREES


def test_a_basetemp_outside_every_system_temp_dir_counts_once_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--basetemp`` (an agent's goes under its scratch tree) can sit outside every temp dir."""
    from kiro_crew.testing import delete_guard

    basetemp = tmp_path / "bt"
    basetemp.mkdir()
    # An account home under tmp_path drops every temp dir above it, this run's basetemp too.
    monkeypatch.setattr(delete_guard, "_account_home", lambda: (tmp_path / "acct").resolve())
    monkeypatch.setattr(delete_guard, "_RUN_TEMP_TREES", [])
    per_test_home = basetemp / "i" / "0-kirocrew-home"
    with pytest.raises(RefusedDelete, match="not inside a temp dir"):
        disposable(per_test_home)
    delete_guard.count_as_temp(basetemp)
    assert disposable(per_test_home) == per_test_home
    with pytest.raises(RefusedDelete, match="not inside a temp dir"):
        disposable(basetemp)
    # A named root that holds the account home is no fence.
    (tmp_path / "acct" / "Documents").mkdir(parents=True)
    delete_guard.count_as_temp(tmp_path)
    with pytest.raises(RefusedDelete, match="not inside a temp dir"):
        disposable(tmp_path / "acct" / "Documents")


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
