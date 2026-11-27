"""A crew chat a conductor closed stays closed, and a close is never lost.

Two crews are in play (rfc-conductor-remote-dispatch.md). The crew half runs the
real ``/api/chat`` routes; the hub half runs the real ``/api/session-control/*``
handlers over the stub peer ``test_session_control_crew`` uses.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)
from test_session_control_crew import (  # noqa: F401 -- ``env`` is a fixture
    INSTANCE,
    _call,
    _conductor_mode,
    _create,
    _no_item_names_it,
    _request,
    _restart,
    env,
)

from kiro_crew import mcp_session_crew as tools
from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import crew_workers_remote, remote_puller
from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.handlers_instances_peer import PeerSlots, PeerSlotsUnavailable
from kiro_crew.mcp_dashboard_session_runs import _run_session_status

SLOT = "chat-9"


@pytest.fixture(autouse=True)
def _fresh_puller():
    remote_puller.reset_for_tests()
    yield
    remote_puller.reset_for_tests()


@pytest.fixture
def _sel():
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        yield


# ── the crew half: a hub conductor's send ──


@pytest.mark.asyncio
async def test_a_conductors_send_never_reopens_a_chat_its_crew_closed(tmp_path, _sel):
    """The hub's send names the chat by key. A key that is not open is refused."""
    ran: list[str] = []

    async def fake_run_chat(st, sl, msg, **kwargs):
        ran.append(msg)

    state = _make_state(tmp_path)
    state.get_or_create_slot(SLOT)._titled = True
    with patch("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat):
        async with TestClient(TestServer(_make_app(state))) as client:
            closed = await client.delete(f"/api/chat/slots/{SLOT}")
            assert closed.status == 200
            assert state.get_slot(SLOT) is None

            resp = await client.post(
                "/api/chat",
                params={"ws": "1"},
                json={"slot": SLOT, "message": "one more thing", "turn_origin": "agent"},
            )
            body = await resp.json()

    assert (resp.status, body.get("code")) == (404, "slot_not_found")
    assert state.get_slot(SLOT) is None
    assert ran == []


@pytest.mark.asyncio
async def test_a_persons_send_still_opens_a_chat_by_key(tmp_path, _sel):
    """Only the agent-origin send is held to an open chat; a tab's own send is as before."""

    async def fake_run_chat(st, sl, msg, **kwargs):
        sl.append("chunk", "ok", "chunk")

    state = _make_state(tmp_path)
    with patch("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", params={"ws": "1"}, json={"slot": SLOT, "message": "hi"}
            )
            assert resp.status in (200, 202)
    assert state.get_slot(SLOT) is not None


# ── the hub half: session_close(crew=) ──


def _switch_on(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)


@pytest.fixture
def kicked(monkeypatch):
    """The background puller, not started: each test runs it by hand."""
    seen: list[object] = []
    monkeypatch.setattr(remote_puller, "ensure_running", seen.append)
    return seen


def _opener(env) -> str:  # noqa: F811 -- shadows the imported fixture name
    (caller,) = crew._record(env.state)
    return caller


def _closes() -> dict[str, list[tuple[str, str]]]:
    return work_ledger.read_crew_closes()


def _deletes(env) -> list[str]:  # noqa: F811
    return [c["path"] for c in env.peer.calls if c["method"] == "DELETE"]


def test_a_close_its_crew_never_got_is_kept_and_made_again(env, monkeypatch, kicked):  # noqa: F811
    """The crew is away when the conductor closes its finished worker. The receipt
    says not closed, the close outlives a hub restart, and the puller lands it."""
    target = _create(env)
    caller = _opener(env)
    _no_item_names_it(monkeypatch)
    env.peer.not_connected = True

    status, body = _call(env, "close", crew="lab", target=target)

    assert (status, body["closed"], body.get("kept")) == (202, False, True)
    assert body["why_code"] == "proxy_peer_not_connected"
    assert "code" not in body and "error" not in body
    assert _closes() == {caller: [(INSTANCE, target)]}
    assert crew.dispatched_by(env.state, caller, INSTANCE, target)
    assert kicked == [env.state]

    # A closed chat takes no more work: the close would cut it off when it lands.
    status, body = _call(env, "send", crew="lab", target=target, message="one more thing")
    assert (status, body["code"]) == (409, "crew_target_closing")

    # Still away: one run asks, gets no answer, and keeps it.
    asyncio.run(remote_puller.run_once(env.state))
    assert _closes() == {caller: [(INSTANCE, target)]}

    _restart(env)
    env.peer.not_connected = False
    env.peer.calls.clear()
    asyncio.run(remote_puller.run_once(env.state))

    assert _deletes(env) == [f"api/chat/slots/{target}"]
    assert _closes() == {}
    assert work_ledger.read_crew_sessions() == {}
    # Nothing left to make again.
    asyncio.run(remote_puller.run_once(env.state))
    assert _deletes(env) == [f"api/chat/slots/{target}"]


@pytest.mark.parametrize(
    "fault, why",
    [
        ("version", "crew_version_mismatch"),
        ("cut", "crew_peer_interrupted"),
        ("crew_500", "crew_peer_refused"),
    ],
)
def test_a_close_no_crew_settled_is_kept_whatever_broke(env, kicked, fault, why):  # noqa: F811
    target = _create(env)
    if fault == "version":
        env.peer.version = "0.0.1"
    elif fault == "cut":
        env.peer.cut_body = True
    else:
        env.peer.reply(
            "DELETE", f"api/chat/slots/{target}", 500, {"error": "busy", "code": "close_failed"}
        )

    status, body = _call(env, "close", crew="lab", target=target)

    assert (status, body.get("kept"), body.get("why_code")) == (202, True, why)
    assert _closes() == {_opener(env): [(INSTANCE, target)]}


def test_a_kept_close_whose_chat_is_already_gone_is_dropped(env, kicked):  # noqa: F811
    """A reply cut off after the crew closed the chat: the next run hears ``gone``."""
    target = _create(env)
    env.peer.cut_body = True
    assert _call(env, "close", crew="lab", target=target)[0] == 202
    env.peer.cut_body = False
    env.peer.reply(
        "DELETE", f"api/chat/slots/{target}", 404, {"error": "gone", "code": "slot_not_found"}
    )

    asyncio.run(remote_puller.run_once(env.state))

    assert _closes() == {}
    assert crew._record(env.state) == {}


def test_a_crew_that_is_away_is_asked_once_per_run(env, monkeypatch, kicked):  # noqa: F811
    first = _create(env)
    env.peer.reply("POST", "api/chat/slots", 200, {"key": "chat-78"})
    assert _call(env, "create", crew="lab")[0] == 200
    env.peer.not_connected = True
    for target in (first, "chat-78"):
        assert _call(env, "close", crew="lab", target=target)[0] == 202
    asked: list[str] = []
    real = env.peer.peer_capability

    async def counted(instance_id, path):
        asked.append(instance_id)
        return await real(instance_id, path)

    monkeypatch.setattr(env.peer, "peer_capability", counted)
    asyncio.run(remote_puller.run_once(env.state))

    assert asked == [INSTANCE]
    assert len(_closes()[_opener(env)]) == 2


def test_a_404_that_names_no_session_leaves_the_chat_on_the_record(
    env, monkeypatch, kicked  # noqa: F811
):
    """A plain 404 is a route the answering side does not serve. The chat may be
    open, so the hub keeps it listed; only ``slot_not_found`` says it is gone."""
    target = _create(env)
    caller = _opener(env)
    _no_item_names_it(monkeypatch)
    env.peer.reply("DELETE", f"api/chat/slots/{target}", 404, "Not Found")
    env.peer.content_type = "text/plain"

    status, body = _call(env, "close", crew="lab", target=target)

    assert (status, body["code"]) == (502, "crew_peer_refused")
    assert "still listed as open" in body["error"]
    assert crew.dispatched_by(env.state, caller, INSTANCE, target)
    assert _closes() == {}

    env.peer.content_type = "application/json"
    env.peer.reply(
        "DELETE", f"api/chat/slots/{target}", 404, {"error": "gone", "code": "slot_not_found"}
    )
    status, body = _call(env, "close", crew="lab", target=target)
    assert (status, body["code"]) == (404, "crew_slot_not_found")
    assert crew._record(env.state) == {}


def test_a_send_to_a_chat_its_crew_closed_tells_the_conductor(env):  # noqa: F811
    target = _create(env)
    env.peer.reply("POST", "api/chat", 404, {"error": "gone", "code": "slot_not_found"})
    status, body = _call(env, "send", crew="lab", target=target, message="one more thing")
    assert (status, body["code"]) == (404, "crew_slot_not_found")


# ── the hub half: session_status ──


class _PeerList:
    """``read_peer_slots`` for the stub crew: its live rows, or a failure."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.fail: Exception | None = None
        self.calls = 0

    async def __call__(self, state: Any, instance_id: str) -> PeerSlots:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return PeerSlots(rows=[dict(r) for r in self.rows], over_cap=0)


@pytest.fixture
def peer_list(monkeypatch):
    fake = _PeerList()
    clock = [1000.0]
    monkeypatch.setattr(crew_workers_remote, "read_peer_slots", fake)
    monkeypatch.setattr(crew_workers_remote, "_now", lambda: clock[0])
    fake.later = lambda: clock.__setitem__(0, clock[0] + 60.0)  # type: ignore[attr-defined]
    return fake


def _status(env, key: str | None = None) -> dict:  # noqa: F811
    req = _request(env.state, key or _key(env.caller), query={})
    resp = asyncio.run(handlers_sc.api_session_control_status(req))
    assert resp.status == 200, resp.body
    return json.loads(resp.body.decode())


def _row(target: str, status: str, answered: bool = True) -> dict:
    return {
        "target": target,
        "crew": INSTANCE,
        "crew_name": "lab",
        "status": status,
        "crew_answered": answered,
    }


def test_session_status_lists_the_chats_a_conductor_opened_on_other_crews(
    env, monkeypatch, peer_list, kicked  # noqa: F811
):
    _switch_on(monkeypatch)
    target = _create(env)
    peer_list.rows = [{"key": target, "title": "the crew's own title", "running": True}]

    body = _status(env)
    assert body["crew_sessions"] == [_row(target, "working")]
    assert body["crew_state"] == "read"
    # State words only: nothing the crew wrote rides the roster.
    assert "the crew's own title" not in json.dumps(body)

    # Another conductor's roster holds none of them.
    other = _conductor_mode(_busy(_slot(env.state, "chat-9")))
    assert _status(env, _key(other))["crew_sessions"] == []

    peer_list.later()
    peer_list.rows = [{"key": target, "running": False}]
    assert _status(env)["crew_sessions"] == [_row(target, "idle")]

    # The crew stops answering: the last state read, marked as such.
    peer_list.later()
    peer_list.fail = PeerSlotsUnavailable("proxy_peer_not_connected", "down", 503, "down")
    assert _status(env)["crew_sessions"] == [_row(target, "idle", answered=False)]

    # Closed while the crew is away: the hub holds the close.
    env.peer.not_connected = True
    assert _call(env, "close", crew="lab", target=target)[0] == 202
    assert _status(env)["crew_sessions"] == [_row(target, "closing", answered=False)]

    # The crew is back and the chat was closed there by someone else.
    peer_list.later()
    peer_list.fail, peer_list.rows = None, []
    env.peer.not_connected = False
    env.peer.reply(
        "DELETE", f"api/chat/slots/{target}", 404, {"error": "gone", "code": "slot_not_found"}
    )
    asyncio.run(remote_puller.run_once(env.state))
    assert _status(env)["crew_sessions"] == []


def test_a_chat_its_crew_closed_reads_closed_and_an_unread_crew_unknown(
    env, monkeypatch, peer_list  # noqa: F811
):
    _switch_on(monkeypatch)
    target = _create(env)
    peer_list.fail = PeerSlotsUnavailable("proxy_peer_not_connected", "down", 503, "down")
    assert _status(env)["crew_sessions"] == [_row(target, "unknown", answered=False)]

    peer_list.later()
    peer_list.fail = None
    assert _status(env)["crew_sessions"] == [_row(target, "closed")]


def test_a_turn_that_may_not_read_a_crew_lists_its_chats_and_asks_no_crew(
    env, monkeypatch, peer_list  # noqa: F811
):
    """The list is the hub's own record. A crew's state is a crew-scoped read, so it
    keeps the ``crew=`` owner arm: a turn the owner did not send asks no crew."""
    _switch_on(monkeypatch)
    target = _create(env)
    monkeypatch.setattr(crew, "owner_unverifiable_reason", lambda slot: "not the owner's turn")

    body = _status(env)

    assert body["crew_sessions"] == [_row(target, "unknown", answered=False)]
    assert body["crew_state"] == "owner_turn_only"
    assert peer_list.calls == 0


def test_switch_off_session_status_is_unchanged(env, peer_list):  # noqa: F811
    _create(env)
    body = _status(env)
    assert "crew_sessions" not in body and "crew_state" not in body


# ── what the conductor reads ──


def _ctx(reply: dict) -> Any:
    client = SimpleNamespace(get=lambda *a, **k: reply, post=lambda *a, **k: reply)
    return SimpleNamespace(client=client, caller_key="dashboard:chat-1")


def test_a_kept_close_is_never_rendered_as_closed():
    out = tools.run_crew_close(
        {"crew": "lab", "target": "chat-77"},
        _ctx(
            {
                "target": "chat-77",
                "crew_name": "lab",
                "closed": False,
                "kept": True,
                "why": "that crew is not connected",
            }
        ),
    )
    assert out.startswith("⏳ NOT closed yet: `chat-77` on crew `lab` is still open")
    assert "that crew is not connected" in out and "`closing`" in out
    assert "Closed `chat-77`" not in out


def test_session_status_renders_the_crew_rows_under_the_local_ones():
    rows = [
        {**_row("chat-77", "working"), "crew_name": "lab"},
        {**_row("chat-78", "closing", answered=False)},
        {**_row("chat-79", "idle", answered=False)},
    ]
    local = {"sessions": [], "tree": "readable"}
    out = _run_session_status({}, _ctx({**local, "crew_sessions": rows}))
    lines = out.splitlines()
    assert lines[0] == "\U0001f4cb You have no sessions open or on record on this machine."
    assert lines[1] == "\U0001f310 3 session(s) you opened on other crews:"
    assert lines[2].endswith(f"`chat-77` on crew `lab` (crew=`{INSTANCE}`) — working")
    assert "closing (the hub holds your close" in lines[3]
    assert lines[4].endswith("idle (last state read; that crew is not answering now)")

    assert _run_session_status({}, _ctx(local)) == (
        "\U0001f4cb You have no sessions open or on record."
    )

    unread = {"crew_state": "owner_turn_only", "crew_sessions": [_row("chat-77", "unknown", False)]}
    lines = _run_session_status({}, _ctx({**local, **unread})).splitlines()
    assert lines[2].endswith("unknown (state not read in this turn)")
    assert lines[3].startswith("This turn may not read another crew, so no crew was asked")


def test_the_status_note_is_advertised_only_while_the_preview_is_on():
    base = [{"name": "session_status", "description": "List.", "inputSchema": {}}]
    on = tools.with_crew_schema(base, True)
    assert "`closing`" in on[0]["description"] and "crew" not in on[0]["inputSchema"]
    assert base[0]["description"] == "List."
    assert tools.with_crew_schema(base, False) == base
