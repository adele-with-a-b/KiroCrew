"""``memory export`` writes every live row of a store, or no file at all.

Pinned: a store past the old 1,000-event cap exports every event; each collection is
read to its end a page at a time, and the file equals one ``json.dumps`` of the full
reads; a write by another connection between two pages neither drops nor repeats a
row; a disk that fills part way leaves the path as it was; no byte goes out while
the read snapshot is open; every export names on stderr what it leaves out, and
every table of a store is either exported or named there. For ``--output``: the
file is created ``0600`` and never through a link at the partial name, a long name
fits, and a symlink, FIFO or device target keeps what it is; a hard-linked file is
written in place whole and keeps its mode, and an in-place target another user owns
is refused before anything is cut, unless it is a character device.
"""

from __future__ import annotations

import argparse
import builtins
import errno
import io
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew import platform_compat
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path
from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_member import create_member_database

_POSIX_ONLY = pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX files and modes")

#: The stderr line every completed export prints, spelled out so a change to it is
#: a change to this test too.
_LEFT_OUT_NOTE = (
    "Note: the export holds every live semantic, episodic and events row. Not in it: "
    "revisions, record metadata, deleted facts, retired episodes, episode vectors, "
    "consolidation receipts, V2 history and its index, and the lessons JSONL file."
)

#: Each table a V1 or V2 store holds, and the payload collections that export it or
#: the words the note names it by. An empty tuple is the store's own bookkeeping,
#: which no person wrote. A new table fails the census until it is named here.
_TABLE_KINDS: dict[str, tuple[str, ...]] = {
    "semantic_memory": ("semantic", "deleted facts"),
    "episodic_memories": ("episodic", "retired episodes", "episode vectors"),
    "memory_items": (
        "semantic",
        "episodic",
        "deleted facts",
        "retired episodes",
        "episode vectors",
    ),
    "memory_events": ("events",),
    "memory_revisions": ("revisions",),
    "memory_record_meta": ("record metadata",),
    "memory_consolidations": ("consolidation receipts",),
    "memory_history": ("V2 history and its index",),
    "memory_fts": ("V2 history and its index",),
    "memory_meta": (),
    "memory_reconcile_dirty": (),
    "memory_reconcile_state": (),
    "member_database": (),
    "schema_version": (),
    "sqlite_sequence": (),
}


def _open_default_store() -> VectorMemoryStore:
    db_path = resolve_store_path(DEFAULT_MEMORY_STORE)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = VectorMemoryStore(db_path=db_path)
    store.init()
    return store


def _seed_default_store(*, episodes: int, facts: int = 0) -> None:
    """Write made-up facts and episodes; each write logs one audit event."""
    store = _open_default_store()
    try:
        for i in range(facts):
            store.set_semantic(
                f"project.made_up_{i:04d}", f"value {i}", 0.9, "test", defer_embedding=True
            )
        for i in range(episodes):
            text = f"Episode {i}: the crew shipped release {i}."
            assert store.write_episodic(text, source="test") is True
    finally:
        store.close()


def _full_reads() -> dict[str, list[dict]]:
    """Every collection as one unpaged read, in the order the export writes it."""
    store = _open_default_store()
    try:
        return {
            "semantic": store.get_all_semantic(),
            "episodic": store.get_episodic_list(limit=-1),
            "events": store.get_events(limit=-1),
        }
    finally:
        store.close()


def _export_to(out: Path | str | None) -> None:
    cc._memory_cmd(
        argparse.Namespace(
            mem_action="export", output=None if out is None else str(out), store=None
        )
    )


def _export(tmp_path: Path) -> dict:
    out = tmp_path / "out" / "export.json"
    _export_to(out)
    return json.loads(out.read_text(encoding="utf-8"))


def _small_pages(monkeypatch: pytest.MonkeyPatch, rows: int) -> None:
    # raising=False: an export that does not page fails on the rows the test
    # asserts, not on this name.
    monkeypatch.setattr(cc, "_EXPORT_PAGE_ROWS", rows, raising=False)


def test_a_store_with_1108_events_exports_all_1108(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_default_store(episodes=1108)
    whole = _full_reads()
    assert len(whole["events"]) == 1108, "the seed must log one event per episode"
    payload = _export(tmp_path)
    assert len(payload["events"]) == 1108
    assert [e["id"] for e in payload["events"]] == [e["id"] for e in whole["events"]]
    assert len(payload["episodic"]) == 1108
    assert "warning" not in capsys.readouterr().err


def test_the_file_is_one_json_dump_of_every_row_across_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_default_store(facts=3, episodes=1107)
    whole = _full_reads()
    assert all(row["embedding"] is None for row in whole["semantic"]), "no vectors written"
    # Three rows a page: every collection spans pages and ends on a full one, so
    # the read after it comes back empty. Past 1,000 events, so a cut shows.
    assert [len(rows) for rows in whole.values()] == [3, 1107, 1110]
    _small_pages(monkeypatch, 3)
    out = tmp_path / "out" / "export.json"
    _export_to(out)
    assert out.read_text(encoding="utf-8") == json.dumps(whole, indent=2, default=str)


def test_a_write_between_pages_neither_drops_nor_repeats_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_default_store(episodes=1005)
    start = _full_reads()
    before = {name: [row["id"] for row in start[name]] for name in ("episodic", "events")}
    _small_pages(monkeypatch, 3)
    read_page = VectorMemoryStore.get_episodic_list
    pages: list[int] = []

    def read_then_write(self: VectorMemoryStore, *args: object, **kwargs: object) -> list[dict]:
        page = read_page(self, *args, **kwargs)  # type: ignore[arg-type]
        if not pages:
            # Another connection retires a row the export has not reached and adds a
            # newer one (two more events), which would shift later offsets.
            other = _open_default_store()
            try:
                assert other.delete_episodic(before["episodic"][-1]) is True
                assert other.write_episodic("A newer episode, written mid-export.", source="t")
            finally:
                other.close()
        pages.append(len(page))
        return page

    monkeypatch.setattr(VectorMemoryStore, "get_episodic_list", read_then_write)
    payload = _export(tmp_path)
    assert [row["id"] for row in payload["episodic"]] == before["episodic"]
    assert [row["id"] for row in payload["events"]] == before["events"]
    assert len(pages) > 1, "the export must have read more than one page"


class _DiskFillsPartWay:
    """A file whose first write lands 16 characters and then fails ``ENOSPC``."""

    def __init__(self, fh: io.TextIOBase) -> None:
        self._fh = fh

    def __enter__(self) -> _DiskFillsPartWay:
        return self

    def __exit__(self, *exc: object) -> None:
        self._fh.close()

    def close(self) -> None:
        self._fh.close()

    def write(self, text: str) -> int:
        self._fh.write(text[:16])
        self._fh.flush()
        raise OSError(errno.ENOSPC, "No space left on device")

    def writelines(self, pieces: object) -> None:
        self.write("".join(pieces))  # type: ignore[arg-type]


def test_a_disk_that_fills_part_way_exits_1_and_leaves_the_path_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_default_store(episodes=5)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = out_dir / "export.json"
    out.write_text("an older export", encoding="utf-8")
    real_open = builtins.open

    def open_on_a_full_disk(file: object, mode: str = "r", *args: object, **kwargs: object):
        fh = real_open(file, mode, *args, **kwargs)  # type: ignore[call-overload]
        in_out_dir = isinstance(file, (str, os.PathLike)) and (
            Path(file).resolve().parent == out_dir.resolve()
        )
        return _DiskFillsPartWay(fh) if in_out_dir and mode != "r" else fh

    # `io.open` is what `Path.write_text` opens with; the builtin is the same object.
    monkeypatch.setattr(builtins, "open", open_on_a_full_disk)
    monkeypatch.setattr(io, "open", open_on_a_full_disk)
    with pytest.raises(SystemExit) as exc_info:
        _export_to(out)
    monkeypatch.undo()
    assert exc_info.value.code == 1
    assert out.read_text(encoding="utf-8") == "an older export"
    assert [p.name for p in out_dir.iterdir()] == ["export.json"], "no partial file is left"
    captured = capsys.readouterr()
    assert f"Error: could not write {out}: No space left on device" in captured.err
    assert "Exported to" not in captured.out


class _UnreadPipe(io.TextIOBase):
    """A stdout whose reader has not read yet.

    Before each piece it lets another connection write and run the TRUNCATE
    checkpoint a closing connection runs, without waiting: it is busy exactly while
    some connection holds a read snapshot of the store.
    """

    def __init__(self, db_path: Path) -> None:
        self.text: list[str] = []
        self.busy: list[int] = []
        self._other = sqlite3.connect(db_path, timeout=0)
        self._other.execute("PRAGMA busy_timeout = 0")
        self._other.execute("CREATE TABLE other_writes (n INTEGER)")
        self._other.commit()

    def write(self, text: str) -> int:
        self._other.execute("INSERT INTO other_writes VALUES (1)")
        self._other.commit()
        self.busy.append(self._other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])
        self.text.append(text)
        return len(text)

    def close(self) -> None:
        self._other.close()
        super().close()


def test_no_byte_goes_to_stdout_while_the_read_snapshot_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_default_store(episodes=5)
    whole = _full_reads()
    pipe = _UnreadPipe(resolve_store_path(DEFAULT_MEMORY_STORE))
    monkeypatch.setattr(sys, "stdout", pipe)
    try:
        _export_to(None)
    finally:
        monkeypatch.undo()
        pipe.close()
    assert pipe.busy, "the export must have written"
    assert pipe.busy == [0] * len(pipe.busy), "a piece went out with the snapshot open"
    payload = json.loads("".join(pipe.text))
    assert [row["id"] for row in payload["episodic"]] == [r["id"] for r in whole["episodic"]]


@pytest.mark.parametrize("to_file", [False, True], ids=["stdout", "output-file"])
def test_every_export_names_what_it_leaves_out_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], to_file: bool
) -> None:
    _seed_default_store(facts=1, episodes=1)
    out = tmp_path / "export.json"
    _export_to(out if to_file else None)
    captured = capsys.readouterr()
    assert captured.err == _LEFT_OUT_NOTE + "\n"
    text = out.read_text(encoding="utf-8") if to_file else captured.out
    assert list(json.loads(text)) == ["semantic", "episodic", "events"]
    if to_file:
        assert captured.out == f"Exported to {out}\n"


def _tables(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        con.close()
    # FTS5's shadow tables belong to their virtual table.
    return {name.split("_fts_")[0] + "_fts" if "_fts_" in name else name for (name,) in rows}


def test_every_table_of_a_store_is_exported_or_named_as_left_out(tmp_path: Path) -> None:
    _open_default_store().close()
    member = tmp_path / "member" / "memory.db"
    create_member_database(member, member_id="made-up-member", store_id="made-up-store")
    tables = _tables(resolve_store_path(DEFAULT_MEMORY_STORE)) | _tables(member)
    unnamed = sorted(tables - set(_TABLE_KINDS))
    assert not unnamed, f"name what memory export does with each new table: {unnamed}"
    exported = ("semantic", "episodic", "events")
    for table in sorted(tables):
        for kind in _TABLE_KINDS[table]:
            assert kind in exported or kind in _LEFT_OUT_NOTE, (table, kind)


@_POSIX_ONLY
def test_an_export_file_is_created_0600_and_a_0600_file_stays_0600(tmp_path: Path) -> None:
    _seed_default_store(episodes=2)
    new = tmp_path / "new.json"
    _export_to(new)
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    old = tmp_path / "old.json"
    old.write_text("an older export", encoding="utf-8")
    old.chmod(0o600)
    _export_to(old)
    assert stat.S_IMODE(old.stat().st_mode) == 0o600
    assert len(json.loads(old.read_text(encoding="utf-8"))["episodic"]) == 2


@_POSIX_ONLY
def test_a_link_at_the_partial_name_fails_the_export_and_is_not_written_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_default_store(episodes=2)
    victim = tmp_path / "victim.txt"
    victim.write_text("not the export's", encoding="utf-8")
    out = tmp_path / "export.json"
    out.write_text("an older export", encoding="utf-8")
    link = tmp_path / f".memory-export.{os.getpid():010d}.partial"
    link.symlink_to(victim)
    with pytest.raises(SystemExit) as exc_info:
        _export_to(out)
    assert exc_info.value.code == 1
    assert victim.read_text(encoding="utf-8") == "not the export's"
    assert out.read_text(encoding="utf-8") == "an older export"
    assert link.is_symlink(), "a name the export did not create is left in place"
    assert f"File exists: {tmp_path.resolve() / link.name}" in capsys.readouterr().err


def test_a_target_name_of_250_bytes_exports(tmp_path: Path) -> None:
    _seed_default_store(episodes=2)
    out = tmp_path / ("a" * 245 + ".json")
    _export_to(out)
    assert len(json.loads(out.read_text(encoding="utf-8"))["episodic"]) == 2


@_POSIX_ONLY
def test_a_symlinked_target_replaces_the_file_it_points_to(tmp_path: Path) -> None:
    _seed_default_store(episodes=2)
    real = tmp_path / "real.json"
    real.write_text("an older export", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    _export_to(link)
    assert link.is_symlink()
    assert len(json.loads(real.read_text(encoding="utf-8"))["episodic"]) == 2


@_POSIX_ONLY
def test_a_fifo_target_is_written_in_place(tmp_path: Path) -> None:
    _seed_default_store(episodes=1)
    fifo = tmp_path / "export.fifo"
    os.mkfifo(fifo)
    # The read end is open first, so the export's open does not wait for a reader;
    # one episode fits the pipe's buffer, so its write does not wait for a read.
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        _export_to(fifo)
        got = b""
        while chunk := os.read(reader, 65536):
            got += chunk
    finally:
        os.close(reader)
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
    assert len(json.loads(got)["episodic"]) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["export.fifo"]


@_POSIX_ONLY
def test_a_character_device_target_is_written_in_place(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_default_store(episodes=1)
    _export_to(os.devnull)
    assert stat.S_ISCHR(os.stat(os.devnull).st_mode)
    assert capsys.readouterr().out == f"Exported to {os.devnull}\n"


@_POSIX_ONLY
def test_a_hard_linked_target_is_written_in_place_whole_and_keeps_its_mode(
    tmp_path: Path,
) -> None:
    _seed_default_store(episodes=1)
    target, other = tmp_path / "export.json", tmp_path / "other-link.json"
    # Longer than the export, so a write that does not cut the file shows.
    target.write_text("an older export " * 10_000, encoding="utf-8")
    os.chmod(target, 0o644)
    os.link(target, other)
    _export_to(target)
    assert len(json.loads(target.read_text(encoding="utf-8"))["episodic"]) == 1
    assert other.read_text(encoding="utf-8") == target.read_text(encoding="utf-8")
    assert os.stat(target).st_nlink == 2
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
    assert sorted(p.name for p in tmp_path.iterdir()) == ["export.json", "other-link.json"]


@pytest.mark.skipif(
    not platform_compat.IS_MACOS,
    reason="macOS reopens /dev/stdout as the shell's own handle; Linux opens the file anew",
)
@pytest.mark.parametrize(
    ("redirect", "flags", "kept"),
    [(">>", os.O_APPEND, "OLD\n"), (">", os.O_TRUNC, "")],
    ids=["append", "truncate"],
)
def test_dev_stdout_onto_a_file_keeps_what_the_shell_redirect_keeps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], redirect: str, flags: int, kept: str
) -> None:
    """``memory export -o /dev/stdout {redirect} file``, on a file that held ``OLD``."""
    _seed_default_store(episodes=1)
    target = tmp_path / "shell-out.txt"
    target.write_text("OLD\n", encoding="utf-8")
    shell_fd = os.open(target, os.O_WRONLY | flags)  # what the shell's redirect opens
    saved_stdout = os.dup(1)
    try:
        os.dup2(shell_fd, 1)
        _export_to("/dev/stdout")
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)
        os.close(shell_fd)
    text = target.read_text(encoding="utf-8")
    assert text.startswith(kept), f"{redirect} keeps what the file held"
    assert len(json.loads(text[len(kept) :])["episodic"]) == 1
    assert capsys.readouterr().out == "Exported to /dev/stdout\n"


def _owned_by_another_user(monkeypatch: pytest.MonkeyPatch, path: Path | str) -> None:
    """``os.fstat`` reports *path*'s inode as another user's; nothing else changes."""
    real_fstat = os.fstat
    planted = os.stat(path)

    def fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if (st.st_dev, st.st_ino) != (planted.st_dev, planted.st_ino):
            return st
        fields = list(st)
        fields[4] = os.geteuid() + 1  # st_uid
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", fstat)


@_POSIX_ONLY
def test_a_hard_linked_target_another_user_owns_is_refused_and_not_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_default_store(episodes=1)
    target = tmp_path / "planted.json"
    target.write_text("someone else's file", encoding="utf-8")
    os.link(target, tmp_path / "their-link.json")
    capsys.readouterr()
    _owned_by_another_user(monkeypatch, target)
    with pytest.raises(SystemExit) as exc:
        _export_to(target)
    assert exc.value.code == 1
    assert capsys.readouterr().err == (
        f"Error: could not write {target}: it is owned by another user, so it is not written\n"
    )
    assert target.read_text(encoding="utf-8") == "someone else's file"


@_POSIX_ONLY
def test_a_fifo_another_user_owns_is_refused_and_gets_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_default_store(episodes=1)
    fifo = tmp_path / "planted.fifo"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        capsys.readouterr()
        _owned_by_another_user(monkeypatch, fifo)
        with pytest.raises(SystemExit) as exc:
            _export_to(fifo)
        got = os.read(reader, 65536)
    finally:
        os.close(reader)
    assert exc.value.code == 1
    assert capsys.readouterr().err == (
        f"Error: could not write {fifo}: it is owned by another user, so it is not written\n"
    )
    assert got == b""


@_POSIX_ONLY
def test_a_character_device_another_user_owns_is_still_written(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Root owns /dev/null on a real host; the fake makes that hold for a root runner too.
    _seed_default_store(episodes=1)
    _owned_by_another_user(monkeypatch, os.devnull)
    _export_to(os.devnull)
    assert capsys.readouterr().out == f"Exported to {os.devnull}\n"
