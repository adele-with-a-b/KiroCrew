"""``crew_mode.stamp_rows``: which rows are Crew Mode roots, and which a root holds.

Pure-function tests on slot payload dicts: ``tree_slots=()`` and ``parents={}`` keep
the walk off the crew log, and the remembered-roots set is swapped per test so no
test reads another's roots.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import crew_mode

ROOT = "root"
CHILD = "child"
PLAIN = "kirocrew-research"


@pytest.fixture(autouse=True)
def _fresh_known_roots(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())


def _row(key: str, agent: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key,
        "agent": agent,
        "agent_kind": "template",
        "mode": "",
        "memory_mode": "persistent",
    }
    row.update(fields)
    return row


def _stamp(rows: list[dict[str, Any]], **kw: Any) -> dict[str, dict[str, Any]]:
    kw.setdefault("tree_slots", ())
    crew_mode.stamp_rows(rows, **kw)
    return {r["key"]: r for r in rows}


def test_a_reopened_child_on_its_conductors_template_is_that_conductors_worker() -> None:
    """A child created without an agent inherits the conductor template. Reopened
    from History, only its saved metadata line names its creator: it is still the
    conductor's worker, never a root of its own."""
    rows = _stamp(
        [
            _row(ROOT, crew_mode.DEFAULT_CREW_MODE_AGENT),
            _row(CHILD, crew_mode.DEFAULT_CREW_MODE_AGENT),
        ],
        saved_creators={CHILD: ROOT},
    )
    assert rows[ROOT][crew_mode.ROOT_FIELD] is True
    assert rows[CHILD][crew_mode.ROOT_FIELD] is False
    assert rows[CHILD][crew_mode.WORKER_FIELD] == ROOT
    assert crew_mode.known_roots() == {ROOT}


def test_a_root_switched_to_a_plain_agent_is_forgotten_and_its_close_frees_its_sessions() -> None:
    """Switching an idle root off every conductor ends its Crew Mode; closing it
    afterwards must not stamp the sessions it created back into Crew Mode."""
    worker = _row("w", "kirocrew-worker", created_by=ROOT)
    rows = _stamp([_row(ROOT, crew_mode.DEFAULT_CREW_MODE_AGENT), dict(worker)])
    assert rows["w"][crew_mode.WORKER_FIELD] == ROOT
    assert crew_mode.known_roots() == {ROOT}

    rows = _stamp([_row(ROOT, PLAIN), dict(worker)])
    assert rows[ROOT][crew_mode.ROOT_FIELD] is False
    assert crew_mode.WORKER_FIELD not in rows["w"]
    assert crew_mode.known_roots() == frozenset()

    rows = _stamp([dict(worker)], parents={})
    assert crew_mode.WORKER_FIELD not in rows["w"]


def test_a_root_that_closes_while_a_root_still_holds_its_workers() -> None:
    """The remembered set shrinks only for a LIVE row that stopped being a root: a
    root whose tab closes stays known, so its workers stay out of the person's list."""
    worker = _row("w", "kirocrew-worker", created_by=ROOT)
    _stamp([_row(ROOT, crew_mode.DEFAULT_CREW_MODE_AGENT), dict(worker)])
    rows = _stamp([dict(worker)], parents={})
    assert rows["w"][crew_mode.WORKER_FIELD] == ROOT
    assert crew_mode.known_roots() == {ROOT}


def test_the_default_conductor_roots_the_worker_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Conductor mode's default agent is a root: ``stamp_rows`` hangs its workers under
    it at every depth, and ``slot_is_crew_mode_root`` answers the same on a live slot."""
    from types import SimpleNamespace

    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    monkeypatch.setattr(crew_mode, "_configured", crew_mode.DEFAULT_CREW_MODE_AGENT)
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: False)
    assert crew_mode.DEFAULT_CREW_MODE_AGENT == "kirocrew-conductor"
    rows = _stamp(
        [
            _row(ROOT, crew_mode.DEFAULT_CREW_MODE_AGENT),
            _row(CHILD, "kirocrew-worker", created_by=ROOT),
            _row("grand", "kirocrew-worker", created_by=CHILD),
        ],
        parents={},
    )
    assert rows[ROOT][crew_mode.ROOT_FIELD] is True
    assert rows[CHILD][crew_mode.WORKER_FIELD] == ROOT
    assert rows["grand"][crew_mode.WORKER_FIELD] == ROOT
    assert rows["grand"][crew_mode.DISPATCHER_FIELD] == CHILD
    live = SimpleNamespace(
        key=ROOT, agent=crew_mode.DEFAULT_CREW_MODE_AGENT, agent_kind="template", mode=""
    )
    assert crew_mode.slot_is_crew_mode_root(live)
    worker = SimpleNamespace(
        key=CHILD, agent="kirocrew-worker", agent_kind="template", mode="", _created_by=ROOT
    )
    assert not crew_mode.slot_is_crew_mode_root(worker)


def test_a_released_conductor_child_is_a_root_by_both_readings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conductor chat another chat created keeps that ``created_by`` after
    ``session_release``; only the session tree's edge is cleared. With settled lineage
    it is a root by the slot list's stamp and by ``slot_is_crew_mode_root`` alike. A
    conductor child nothing released stays its creator's worker by both, and a chat
    the tree never saw created (a cron job's) keeps its birth stamp."""
    from types import SimpleNamespace

    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    monkeypatch.setattr(crew_mode, "_configured", crew_mode.DEFAULT_CREW_MODE_AGENT)
    conductor = crew_mode.DEFAULT_CREW_MODE_AGENT
    tracked = ("maker", "released", "held", "cron")
    rows = _stamp(
        [
            _row("maker", PLAIN),
            _row("released", conductor, created_by="maker", parent=None),
            _row("held", conductor, created_by="maker", parent={"slot": "maker", "key": "maker"}),
            _row("cron", conductor, created_by="cron:job-1", parent=None),
        ],
        settled=True,
        tree_slots=tracked,
        released=("released",),
    )
    assert rows["released"][crew_mode.ROOT_FIELD] is True
    assert rows["held"][crew_mode.ROOT_FIELD] is False
    assert rows["cron"][crew_mode.ROOT_FIELD] is False

    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: True)
    monkeypatch.setattr(crew_mode, "persisted_tree_slots", lambda: frozenset(tracked))
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {"held": "maker"})
    monkeypatch.setattr(crew_mode, "released_slots", lambda: frozenset({"released"}))

    def live(key: str, created_by: str = "maker") -> SimpleNamespace:
        return SimpleNamespace(
            key=key, agent=conductor, agent_kind="template", mode="", _created_by=created_by
        )

    assert crew_mode.slot_is_crew_mode_root(live("released"))
    assert not crew_mode.slot_is_crew_mode_root(live("held"))
    assert not crew_mode.slot_is_crew_mode_root(live("cron", "cron:job-1"))
    # The slot list reads the same release set when no caller hands one in.
    rows = _stamp(
        [
            _row("released", conductor, created_by="maker", parent=None),
            _row("held", conductor, created_by="maker", parent={"slot": "maker", "key": "maker"}),
        ],
        settled=True,
        tree_slots=tracked,
    )
    assert rows["released"][crew_mode.ROOT_FIELD] is True
    assert rows["held"][crew_mode.ROOT_FIELD] is False
    # Until the lineage settles the birth stamp still holds the released chat.
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: False)
    monkeypatch.setattr(crew_mode, "released_slots", lambda: frozenset())
    assert not crew_mode.slot_is_crew_mode_root(live("released"))
    rows = _stamp([_row("released", conductor, created_by="maker", parent=None)])
    assert rows["released"][crew_mode.ROOT_FIELD] is False
    # A closed one is re-learned from its saved line only with the release read.
    meta = {"agent": conductor, "created_by": "maker"}
    assert not crew_mode.saved_row_is_root(meta)
    assert crew_mode.saved_row_is_root(meta, released=True)


def test_released_slots_are_the_slots_whose_newest_decision_is_a_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a release record makes a parentless node released: a cron-opened chat's
    node has no parent and no release, a release record on a node that now has a
    parent does not count, and nothing is released while the lineage is unsettled."""
    from types import SimpleNamespace

    from kiro_crew.crew_log import session_tree_projection

    node = SimpleNamespace
    tree = SimpleNamespace(
        nodes=lambda: {
            "released": node(parent_slot=None),
            "held": node(parent_slot="maker"),
            "cron": node(parent_slot=None),
        },
        released=lambda: frozenset({"released", "held"}),
    )
    monkeypatch.setattr(session_tree_projection, "projection", lambda: tree)
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: True)
    assert crew_mode.released_slots() == frozenset({"released"})
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: False)
    assert crew_mode.released_slots() == frozenset()
