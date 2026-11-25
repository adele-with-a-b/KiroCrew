"""A crew that does not answer, and where a worker runs.

Three things a conductor needs when a linked crew's machine is reachable and its
Crew is not (stopped, or mid-install):

* the board says the crew is down and since when, although the tunnel reads
  connected, and the retire clock runs, so the item becomes redispatchable;
* a work-item create that no crew took says the item is still unbound and how to
  run it on this machine;
* every bound row says which machine its worker runs on (``runs_on``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import test_remote_dispatch_hub_ingest as hub
import work_ledger_projection_helpers as helpers
from test_mcp_session_crew import _Client, _ctx
from test_remote_dispatch_hub_ingest import CREW, PEER_SLOT, StubCrew, _read, _remote_item, _row
from test_remote_dispatch_retire import (  # noqa: F401
    _closes_done,
    _no_background_puller,
    clock,
    peer,
)
from work_ledger_projection_helpers import CONDUCTOR

from kiro_crew import mcp_session_crew as tools
from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_ingest, work_ledger_read

_isolated = helpers._isolated
recorded = hub.recorded


def _stop_the_crew(stub: StubCrew) -> None:
    """The tunnel stays connected; nothing behind it answers the version read."""

    stub.version_reads = 0  # type: ignore[attr-defined]

    async def _no_answer(instance_id: str, path: str):
        stub.version_reads += 1  # type: ignore[attr-defined]
        return False, {"code": "capability_unreachable"}

    stub.peer_capability = _no_answer  # type: ignore[method-assign]


def _answer_with(stub: StubCrew, code: str) -> None:
    """The crew is up and answers the version read with a refusal."""

    async def _refused(instance_id: str, path: str):
        return False, {"code": code}

    stub.peer_capability = _refused  # type: ignore[method-assign]


def _start_the_crew(stub: StubCrew) -> None:
    del stub.peer_capability


# ── the board and the retire clock ───────────────────────────────────────────


def test_a_stopped_crew_behind_a_live_tunnel_reads_down(recorded, peer):  # noqa: F811
    async def go() -> None:
        item_id = await _remote_item(peer)
        _stop_the_crew(peer)
        row = _row(await _read(peer), item_id)
        assert peer.state == "connected"
        assert row["crew_state"] == "error" and row["crew_down_since"]
        assert row["remote_error"] == "crew_version_unknown"
        since = row["crew_down_since"]
        again = _row(await _read(peer, "compact=true"), item_id)
        assert again["crew_down_since"] == since

    asyncio.run(go())


def test_a_crew_that_stays_stopped_past_the_timeout_is_retired(recorded, peer):  # noqa: F811
    async def go() -> None:
        item_id = await _remote_item(peer)
        _stop_the_crew(peer)
        await _read(peer)
        peer.clock.now += 29 * 60  # type: ignore[attr-defined]
        early = _row(await _read(peer), item_id)
        assert early["worker_instance_id"] == CREW and "redispatch_due" not in early
        peer.clock.now += 2 * 60  # type: ignore[attr-defined]
        row = _row(await _read(peer), item_id)
        assert row["redispatch_due"] is True and row["retired_crews"] == [CREW]
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.state == "open" and item.worker_session_key is None
        # The old slot is not closed while its crew still does not answer: the
        # next read, inside the freshness window, does not even ask the crew.
        asked = peer.version_reads  # type: ignore[attr-defined]
        await remote_ingest.ingest_board(hub._state(peer), CONDUCTOR)
        await _closes_done()
        assert peer.version_reads == asked  # type: ignore[attr-defined]
        assert not remote_ingest._CLOSE_TRIED
        assert all(call["method"] != "DELETE" for call in peer.calls)

    asyncio.run(go())


def test_the_retired_slot_is_closed_once_the_stopped_crew_answers(recorded, peer):  # noqa: F811
    """A close tried while the crew is still stopped is not the one try."""

    async def go() -> None:
        item_id = await _remote_item(peer)
        _stop_the_crew(peer)
        await _read(peer)
        peer.clock.now += 31 * 60  # type: ignore[attr-defined]
        assert _row(await _read(peer), item_id)["redispatch_due"] is True
        state = hub._state(peer)
        # No open item is bound there any more, so nothing refreshes the read.
        peer.clock.now += remote_ingest._UNANSWERED_FRESH_SECS + 1  # type: ignore[attr-defined]
        await remote_ingest.ingest_board(state, CONDUCTOR)
        await _closes_done()
        assert all(call["method"] != "DELETE" for call in peer.calls)
        assert not remote_ingest._CLOSE_TRIED
        _start_the_crew(peer)
        peer.clock.now += remote_ingest._UNANSWERED_FRESH_SECS + 1  # type: ignore[attr-defined]
        await remote_ingest.ingest_board(state, CONDUCTOR)
        await _closes_done()
        deletes = [call["path"] for call in peer.calls if call["method"] == "DELETE"]
        assert deletes == [f"api/chat/slots/{PEER_SLOT}"]

    asyncio.run(go())


@pytest.mark.parametrize(
    "code",
    [
        "capability_unauthorized",
        "capability_peer_too_old",
        "capability_peer_revalidating",
        "capability_peer_refused",
        "capability_malformed_reply",
    ],
)
def test_a_crew_that_answered_with_a_refusal_is_not_down(recorded, peer, code):  # noqa: F811
    """Only no answer at all starts the retire clock."""

    async def go() -> None:
        item_id = await _remote_item(peer)
        _stop_the_crew(peer)
        await _read(peer)
        _answer_with(peer, code)
        row = _row(await _read(peer), item_id)
        assert row["remote_error"] == "crew_version_unknown"
        assert row["crew_state"] == "connected" and row["crew_down_since"] is None
        assert CREW not in remote_ingest._UNANSWERED
        peer.clock.now += 31 * 60  # type: ignore[attr-defined]
        late = _row(await _read(peer), item_id)
        assert late["worker_instance_id"] == CREW and "redispatch_due" not in late

    asyncio.run(go())


def test_a_crew_that_answers_again_clears_the_clock(recorded, peer):  # noqa: F811
    async def go() -> None:
        item_id = await _remote_item(peer)
        _stop_the_crew(peer)
        await _read(peer)
        assert any(key[1] == item_id for key in remote_ingest._DOWN_SEEN)
        _start_the_crew(peer)
        peer.clock.now += 31 * 60  # type: ignore[attr-defined]
        row = _row(await _read(peer), item_id)
        assert row["crew_state"] == "connected" and row["crew_down_since"] is None
        assert row["worker_instance_id"] == CREW and "redispatch_due" not in row
        assert not any(key[1] == item_id for key in remote_ingest._DOWN_SEEN)
        assert CREW not in remote_ingest._UNANSWERED

    asyncio.run(go())


def test_one_unanswered_read_stands_only_while_it_is_fresh(peer):  # noqa: F811
    """A pull that stops reaching the version read says nothing about the crew."""
    remote_ingest._UNANSWERED[CREW] = peer.clock.now  # type: ignore[attr-defined]
    assert remote_ingest._crew_up(peer, CREW) is False
    peer.clock.now += remote_ingest._UNANSWERED_FRESH_SECS + 1  # type: ignore[attr-defined]
    assert remote_ingest._crew_up(peer, CREW) is True
    peer.state = "disconnected"
    assert remote_ingest._crew_up(peer, CREW) is False


def test_a_version_skew_is_still_an_error_and_not_an_outage(recorded, peer):  # noqa: F811
    async def go() -> None:
        item_id = await _remote_item(peer)
        _stop_the_crew(peer)
        await _read(peer)
        _start_the_crew(peer)
        peer.version = "0.0.1"
        row = _row(await _read(peer), item_id)
        assert row["remote_error"] == "crew_version_mismatch"
        assert row["crew_state"] == "connected" and row["crew_down_since"] is None
        assert CREW not in remote_ingest._UNANSWERED
        peer.clock.now += 31 * 60  # type: ignore[attr-defined]
        late = _row(await _read(peer), item_id)
        assert late["worker_instance_id"] == CREW and "redispatch_due" not in late

    asyncio.run(go())


# ── which machine a worker runs on ───────────────────────────────────────────


def test_a_remote_row_names_its_crew_and_a_local_row_says_local(
    recorded, peer, monkeypatch  # noqa: F811
):
    monkeypatch.setattr(work_ledger_read, "_crew_names", lambda state: {CREW: "lab"})

    async def go() -> None:
        item_id = await _remote_item(peer)
        for query in ("", "compact=true"):
            assert _row(await _read(peer, query), item_id)["runs_on"] == "lab"

    asyncio.run(go())


def test_a_local_worker_reads_local_and_an_unnamed_crew_reads_its_id(
    recorded, peer, monkeypatch  # noqa: F811
):
    monkeypatch.setattr(work_ledger_read, "_crew_names", lambda state: {})

    async def go() -> None:
        local_id = await helpers._board()
        for query in ("", "compact=true"):
            assert _row(await _read(peer, query), local_id)["runs_on"] == "local"

    asyncio.run(go())
    item = work_ledger.WorkItem(worker_session_key=PEER_SLOT, worker_instance_id=CREW)
    assert work_ledger_read._runs_on(item, {}) == CREW


def test_an_unbound_row_names_no_machine(recorded, peer):  # noqa: F811
    async def go() -> None:
        status, body = await helpers._record(
            CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1}
        )
        assert status == 200, body
        status, body = await helpers._record(
            CONDUCTOR, {"action": "create", "title": "t", "acceptance": helpers.ACCEPTANCE}
        )
        assert status == 200, body
        row = _row(await _read(peer), body["item"]["item_id"])
        assert "runs_on" not in row

    asyncio.run(go())


def test_an_unreadable_registry_never_fails_the_read():
    class _Broken:
        @property
        def instances_registry(self) -> Any:
            raise RuntimeError("no registry")

    assert work_ledger_read._crew_names(_Broken()) == {}  # type: ignore[arg-type]


# ── the create reply: run it here ────────────────────────────────────────────

_ITEM = "it_0000beef"


@pytest.mark.parametrize(
    "code",
    ["crew_busy", "crew_none_available", "proxy_peer_not_connected", "crew_version_unknown"],
)
def test_a_create_no_crew_took_says_how_to_run_it_here(code: str):
    refuse = {"error": "that crew is short on memory", "code": code}
    out = tools.run_crew_create({"crew": "lab", "work_item": _ITEM}, _ctx(_Client(refuse=refuse)))
    assert out.startswith(
        "Error: could not create a session on that crew: that crew is short on memory. "
        f"{_ITEM} is still open and unbound."
    )
    assert "session_create without crew or work_item" in out
    assert "work_ledger_record action=bind" in out


def test_a_refusal_that_may_have_reached_the_crew_offers_no_local_run():
    refuse = {"error": "nope", "code": "crew_peer_lacks_work_items"}
    out = tools.run_crew_create({"crew": "lab", "work_item": _ITEM}, _ctx(_Client(refuse=refuse)))
    assert out == "Error: could not create a session on that crew: nope"


def test_a_create_with_no_work_item_offers_no_local_run():
    refuse = {"error": "busy", "code": "crew_busy"}
    out = tools.run_crew_create({"crew": "lab"}, _ctx(_Client(refuse=refuse)))
    assert out == "Error: could not create a session on that crew: busy"
