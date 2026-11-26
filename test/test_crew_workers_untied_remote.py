"""A Conductor's chats on another crew that no ledger item names, on its worker list.

``session_create(crew=)`` with no ``work_item`` opens a chat only the hub's dispatch
record knows. Every such chat gets a row under the Conductor whose crew opened it,
with the title and state its crew last reported, and the pair the page opens it
with. Every case names the rule it guards and fails when that rule is removed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_mode
from kiro_crew.dashboard.handlers import crew_workers, crew_workers_remote, remote_dispatch_routes
from kiro_crew.dashboard.handlers_instances_peer import PeerSlots, PeerSlotsUnavailable

sys.path.insert(0, str(Path(__file__).parent))

from redaction_structured_cases import github_classic  # noqa: E402

INSTANCE = "inst-1"
SLOT = "chat-9-1792540567"


class _State(SimpleNamespace):
    """A gateway state the dispatch record can key: weakly referenced, hashed by identity."""

    __hash__ = object.__hash__


def _row(key: str, **over: Any) -> dict[str, Any]:
    return {"key": key, "agent": "kirocrew", "agent_kind": "template", "parent": None, **over}


_TREE = [
    _row("root", agent="kirocrew-conductor"),
    _row("w1", created_by="root", title="Haiku one", running=True),
    _row("other-root", agent="kirocrew-conductor"),
]


def _state(rows: list[dict[str, Any]] = _TREE) -> _State:
    def serialize(**_k: Any) -> list[dict[str, Any]]:
        out = [dict(r) for r in rows]
        crew_mode.stamp_rows(out)
        return out

    async def serialize_slots_yielding(**kwargs: Any) -> list[dict[str, Any]]:
        return serialize(**kwargs)

    return _State(
        serialize_slots=serialize,
        serialize_slots_yielding=serialize_slots_yielding,
        get_slot=lambda key: None,
        instances_registry=SimpleNamespace(
            list=lambda: [SimpleNamespace(id=INSTANCE, name="Second Mac")]
        ),
    )


class _Peer:
    """``read_peer_slots`` for one crew: its live rows, or a failure, counted."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.over_cap = 0
        self.fail: Exception | None = None
        self.calls: list[str] = []
        self.hold: asyncio.Event | None = None

    async def __call__(self, state: Any, instance_id: str) -> PeerSlots:
        self.calls.append(instance_id)
        if self.hold is not None:
            await self.hold.wait()
        if self.fail is not None:
            raise self.fail
        return PeerSlots(rows=[dict(r) for r in self.rows], over_cap=self.over_cap)


@pytest.fixture
def peer(monkeypatch):
    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(crew_mode, "_known_roots", set())
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
    monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _allow)
    monkeypatch.setattr(crew_workers, "_closed_workers", lambda root, live: [])
    monkeypatch.setattr(crew_workers, "_ledger_items", lambda root: {})
    crew_workers._reset_for_tests()
    clock = [1000.0]
    monkeypatch.setattr(crew_workers_remote, "_now", lambda: clock[0])
    fake = _Peer([{"key": SLOT, "title": "Screenshot pass on every theme", "running": True}])
    monkeypatch.setattr(crew_workers_remote, "read_peer_slots", fake)
    fake.clock = clock  # type: ignore[attr-defined]
    yield fake
    crew_workers._reset_for_tests()


async def _workers(state: Any, root: str = "root") -> dict[str, dict[str, Any]]:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(
        "GET", f"/api/chat/slots/{root}/crew-workers", app=app, match_info={"slot": root}
    )
    resp = await crew_workers.api_crew_workers(req)
    assert resp.status == 200, resp.text
    return {w["key"]: w for w in json.loads(resp.text)["workers"]}


def _untied_key(slot: str = SLOT) -> str:
    return f"{crew_workers.REMOTE_KEY_PREFIX}{INSTANCE}:{slot}"


@pytest.mark.asyncio
async def test_a_chat_opened_on_a_crew_with_no_task_is_a_row_under_its_conductor(peer):
    state = _state()
    remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, SLOT)
    by_key = await _workers(state)
    row = by_key[_untied_key()]
    assert row["title"] == "Screenshot pass on every theme"
    assert (row["status"], row["group"]) == ("working", "working")
    # No task: no item and no line; the page says "No task linked" there.
    assert (row["ledger_item"], row["summary"]) == ("", "")
    assert row["remote_crew"] == "Second Mac"
    # What the page opens the chat with: the crew and the crew's own slot key.
    assert (row["remote_instance"], row["remote_slot"]) == (INSTANCE, SLOT)
    assert (row["created_by"], row["dispatched_by"]) == ("root", "root")
    # A local worker is unchanged.
    assert "remote_crew" not in by_key["w1"] and "remote_slot" not in by_key["w1"]


@pytest.mark.asyncio
async def test_a_chat_opened_before_a_hub_restart_is_still_listed(peer):
    """The record's stored copy is read through its owner's loader: a gateway state
    that recorded nothing itself still lists the chat its data home keeps."""
    from kiro_crew import work_ledger

    work_ledger.write_crew_sessions("root", [(INSTANCE, SLOT)])
    row = (await _workers(_state()))[_untied_key()]
    assert (row["title"], row["remote_slot"]) == ("Screenshot pass on every theme", SLOT)


@pytest.mark.asyncio
async def test_a_chat_its_task_already_lists_has_one_row_and_that_row_opens(peer, monkeypatch):
    """A chat born bound to an item is that item's row: never listed twice, and the
    item's row carries the same pair, so a click opens it too."""
    state = _state()
    remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, SLOT)
    item = SimpleNamespace(
        item_id="it_r",
        worker_instance_id=INSTANCE,
        worker_session_key=SLOT,
        is_terminal=False,
        retired_bindings=[],
    )
    tied = crew_workers.LedgerFacts(
        "it_r", "Fix CI", "", crew=INSTANCE, board="root", title="Fix CI", work_item=item
    )
    monkeypatch.setattr(
        crew_workers, "_ledger_items", lambda root: {crew_workers.REMOTE_KEY_PREFIX + "it_r": tied}
    )
    by_key = await _workers(state)
    remote = [w for w in by_key.values() if w.get("remote_crew")]
    assert [w["key"] for w in remote] == [crew_workers.REMOTE_KEY_PREFIX + "it_r"]
    assert (remote[0]["remote_instance"], remote[0]["remote_slot"]) == (INSTANCE, SLOT)
    assert peer.calls == []


@pytest.mark.asyncio
async def test_a_bound_row_with_an_unusable_peer_key_offers_no_open(peer, monkeypatch):
    item = SimpleNamespace(
        item_id="it_r", worker_instance_id=INSTANCE, worker_session_key="../x", is_terminal=False
    )
    tied = crew_workers.LedgerFacts(
        "it_r", "Fix CI", "", crew=INSTANCE, board="root", title="Fix CI", work_item=item
    )
    monkeypatch.setattr(
        crew_workers, "_ledger_items", lambda root: {crew_workers.REMOTE_KEY_PREFIX + "it_r": tied}
    )
    row = (await _workers(_state()))[crew_workers.REMOTE_KEY_PREFIX + "it_r"]
    assert "remote_slot" not in row and "remote_instance" not in row


_BOUND = crew_workers.REMOTE_KEY_PREFIX + "it_r"


def _bind_finished_task(monkeypatch, chat: dict[str, bool]) -> None:
    """One remote item, its task finished, whose stored record says ``chat["closed"]``."""
    from kiro_crew import work_ledger

    item = SimpleNamespace(
        item_id="it_r",
        worker_instance_id=INSTANCE,
        worker_session_key=SLOT,
        is_terminal=True,
        retired_bindings=[],
    )
    monkeypatch.setattr(work_ledger, "remote_liveness", lambda board, it: (False, chat["closed"]))
    tied = crew_workers.LedgerFacts(
        "it_r", "Fix CI", "", crew=INSTANCE, board="root", title="Fix CI", work_item=item
    )
    monkeypatch.setattr(crew_workers, "_ledger_items", lambda root: {_BOUND: tied})


@pytest.mark.asyncio
async def test_a_finished_tasks_chat_still_open_on_its_crew_keeps_its_pair(peer, monkeypatch):
    """A finished task is not a gone chat: the row is closed and done, and the chat
    its crew still holds opens from it."""
    _bind_finished_task(monkeypatch, {"closed": False})
    row = (await _workers(_state()))[_BOUND]
    assert (row["status"], row["group"], row["remote_crew"]) == ("closed", "done", "Second Mac")
    assert (row["remote_instance"], row["remote_slot"]) == (INSTANCE, SLOT)


@pytest.mark.asyncio
async def test_a_chat_known_closed_on_its_crew_carries_no_pair_bound_or_untied(peer, monkeypatch):
    """Only the chat decides: a bound chat its stored record marks closed and an
    untied chat its crew answered without carry no pair, and the same finished
    task's chat, once its record says open, carries one."""
    chat = {"closed": True}
    _bind_finished_task(monkeypatch, chat)
    state = _state()
    untied = "chat-8-1792540500"
    remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, untied)
    by_key = await _workers(state)
    for key in (_BOUND, _untied_key(untied)):
        assert by_key[key]["status"] == "closed"
        assert "remote_slot" not in by_key[key] and "remote_instance" not in by_key[key]
    chat["closed"] = False
    row = (await _workers(state))[_BOUND]
    assert (row["remote_instance"], row["remote_slot"]) == (INSTANCE, SLOT)


@pytest.mark.asyncio
async def test_only_the_conductor_that_opened_a_chat_lists_it(peer):
    """Only a conductor mode root opens a chat on a crew: each root's chat is listed
    under that root, dispatched by it, and never under another root."""
    state = _state()
    remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, SLOT)
    remote_dispatch_routes._record_dispatch(state, "other-root", INSTANCE, "chat-8-1792540500")
    mine = {k: w for k, w in (await _workers(state)).items() if w.get("remote_crew")}
    assert [(k, w["dispatched_by"]) for k, w in mine.items()] == [(_untied_key(), "root")]
    other = await _workers(state, "other-root")
    assert [(k, w["dispatched_by"]) for k, w in other.items()] == [
        (_untied_key("chat-8-1792540500"), "other-root")
    ]


@pytest.mark.asyncio
async def test_each_state_reads_from_the_crews_own_row(peer):
    state = _state()
    for slot in ("chat-1-1792540001", "chat-2-1792540002", "chat-3-1792540003", SLOT):
        remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, slot)
    peer.rows = [
        {"key": "chat-1-1792540001", "title": "Asks", "running": True, "pending_approval": True},
        {"key": "chat-2-1792540002", "title": "Done talking"},
        {"key": "chat-3-1792540003", "title": "Has a question", "needs_input": True},
    ]
    by_key = await _workers(state)
    got = {k: (w["status"], w["group"]) for k, w in by_key.items() if w.get("remote_crew")}
    assert got == {
        _untied_key("chat-1-1792540001"): ("needs_approval", "approval"),
        _untied_key("chat-2-1792540002"): ("idle", "waiting"),
        _untied_key("chat-3-1792540003"): ("asking", "with_conductor"),
        # The crew answered without it: it was closed there.
        _untied_key(): ("closed", "done"),
    }
    # A closed chat has nothing to open; an open one has.
    assert "remote_slot" not in by_key[_untied_key()]
    assert by_key[_untied_key("chat-2-1792540002")]["remote_slot"] == "chat-2-1792540002"


@pytest.mark.asyncio
async def test_a_list_cut_at_its_row_cap_never_closes_a_chat_past_the_cut(peer):
    """A crew whose list was cut cannot say a chat it left out is gone: that chat
    stays open to a click and is not ``closed``; the chats it did send keep their state."""
    state = _state()
    for slot in ("chat-2-1792540002", SLOT):
        remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, slot)
    peer.rows = [{"key": "chat-2-1792540002", "title": "Done talking"}]
    peer.over_cap = 1
    by_key = await _workers(state)
    cut = by_key[_untied_key()]
    assert (cut["status"], cut["group"], cut["remote_slot"]) == ("idle", "waiting", SLOT)
    assert by_key[_untied_key("chat-2-1792540002")]["title"] == "Done talking"


@pytest.mark.asyncio
async def test_a_title_from_the_crew_is_redacted(peer):
    state = _state()
    remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, SLOT)
    secret = github_classic(seed=44)
    peer.rows = [{"key": SLOT, "title": f"Use {secret} now"}]
    row = (await _workers(state))[_untied_key()]
    assert secret not in row["title"]


@pytest.mark.asyncio
async def test_the_crew_is_read_once_per_window_and_the_last_state_stays(peer):
    state = _state()
    remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, SLOT)
    await _workers(state)
    await _workers(state)
    assert peer.calls == [INSTANCE]
    # Past the window it is read again; a failed read keeps what was last read.
    peer.clock[0] += crew_workers_remote.PEER_READ_TTL_SECS + 1
    peer.fail = PeerSlotsUnavailable("proxy_peer_not_connected", "down", 503, "down")
    row = (await _workers(state))[_untied_key()]
    assert peer.calls == [INSTANCE, INSTANCE]
    assert (row["title"], row["status"]) == ("Screenshot pass on every theme", "working")
    # A failed read is not retried inside the window either.
    await _workers(state)
    assert len(peer.calls) == 2


@pytest.mark.asyncio
async def test_a_new_chat_is_read_at_once_not_at_the_next_window(peer):
    state = _state()
    remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, SLOT)
    await _workers(state)
    remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, "chat-5-1792540005")
    peer.rows.append({"key": "chat-5-1792540005", "title": "Second one"})
    row = (await _workers(state))[_untied_key("chat-5-1792540005")]
    assert row["title"] == "Second one"
    assert len(peer.calls) == 2


@pytest.mark.asyncio
async def test_a_crew_never_read_still_lists_its_chat(peer):
    state = _state()
    remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, SLOT)
    peer.fail = PeerSlotsUnavailable("proxy_peer_not_connected", "down", 503, "down")
    row = (await _workers(state))[_untied_key()]
    # Its short key stands in for the title; nothing says it runs.
    assert (row["title"], row["status"], row["group"]) == ("chat-9", "idle", "waiting")


@pytest.mark.asyncio
async def test_a_slow_crew_never_holds_the_list(peer, monkeypatch):
    # 0: the list's wait on the crew has already run out, so no timer runs; the
    # crew holds its answer until the test lets it go.
    monkeypatch.setattr(crew_workers_remote, "PEER_READ_TIMEOUT_SECS", 0)
    state = _state()
    remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, SLOT)
    peer.hold = asyncio.Event()
    by_key = await asyncio.wait_for(_workers(state), 5)
    assert by_key[_untied_key()]["status"] == "idle"
    # The list came back while the crew's read is still held, and the read goes
    # on: once the crew answers, the next list has its state.
    task = crew_workers_remote._CACHE[state][INSTANCE].task
    assert task is not None and not task.done()
    peer.hold.set()
    await asyncio.wait_for(task, 5)
    assert (await _workers(state))[_untied_key()]["status"] == "working"
    assert peer.calls == [INSTANCE]


@pytest.mark.asyncio
async def test_a_bot_thread_lists_no_crew_chat(peer, monkeypatch):
    monkeypatch.setattr(crew_workers, "_live_root", lambda rows, root: ("mate", {"key": root}))
    state = _state()
    remote_dispatch_routes._record_dispatch(state, "root", INSTANCE, SLOT)
    assert _untied_key() not in await _workers(state)
    assert peer.calls == []
