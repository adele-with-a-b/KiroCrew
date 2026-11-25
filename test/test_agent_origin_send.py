"""An agent-origin ``POST /api/chat`` send holds no person-only right.

A hub conductor drives a remote worker through the peer's own ``POST /api/chat``
over the owner-token proxy (rfc-conductor-remote-dispatch §1, §4). The body key
``turn_origin`` marks such a send. These pin each person-only grant the send
path feeds:

* the started turn's ``_directive_user_origin``, which the turn turns into
  ``_turn_user_sent`` (guide start, change-card propose, ``update_self``) and
  into ``producer_is_user_facing`` (user-origin directives such as
  ``suggest_followup``);
* ``_human_seen``, the attendance mark that gives an app-owned tab the person's
  approval window;
* the row's ``HUMAN_TURN_META_KEY`` and the autonudge "user wins" signal;
* the busy-slot queue entry, the sub-agent hold entry and the steer.

Each runs marked and unmarked: a malformed value reads as marked, and a send
without the key behaves exactly as today.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from guide_route_helpers import CREWMATE, FakeState, agent, run_guide_app

from kiro_crew.dashboard import agent_origin
from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import async_wait_until

#: Values that must all read as agent-origin: the one
#: defined value, an unknown word, and malformed shapes.
_MARKS = ["agent", "person", "", None, 1, True, {"x": 1}, ["agent"]]


@pytest.fixture
def _sel():
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        yield


async def _send(state, body: dict) -> tuple[dict | None, Any]:
    """POST /api/chat on slot ``s1``; the kwargs the started turn got, and the response."""
    seen: dict[str, Any] = {}

    async def fake_run_chat(st, sl, msg, **kwargs):
        seen.update(kwargs)
        sl.append("chunk", "ok", "chunk")

    state.get_or_create_slot("s1")._titled = True
    with patch("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "s1", "message": "hi", **body}, timeout=None
            )
            if resp.content_type != "text/event-stream":
                return seen or None, await resp.json()
            # The turn's SSE stream stays open for later frames; the first chunk
            # proves the turn ran, which is all these tests read.
            async for _chunk in resp.content.iter_any():
                break
            resp.close()
            await async_wait_until(lambda: bool(seen), describe=lambda: "turn never started")
            return seen, None


async def _send_through_the_drain(state, body: dict) -> tuple[dict, list[str]]:
    """POST /api/chat on ``s1`` with the real queue drain; the reply and each turn's message.

    A turn the route's drain starts is awaited before this returns, so an empty
    list means no turn began on the queued entry.
    """
    ran: list[str] = []

    async def fake_run_chat(st, sl, msg, **kwargs):
        ran.append(msg)

    slot = state.get_or_create_slot("s1")
    slot._titled = True
    with (
        patch("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat),
        patch("kiro_crew.dashboard.chat_runner._run_chat", fake_run_chat),
    ):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "hi", **body})
            reply = await resp.json()
        if isinstance(slot.task, asyncio.Task):
            await slot.task
    return reply, ran


def _mirror_store(monkeypatch) -> list[str | None]:
    """The channel-mirror read the containment stamp and the drain's sweep share.

    It reads "not mirrored" until the test sets the cell to ``None``: the session
    store could not answer, which the sweep counts as a newly held constraint, so
    it drops the queued entry instead of starting it.
    """
    from kiro_crew.dashboard.session_control_parts import delivery_containment

    cell: list[str | None] = [""]
    monkeypatch.setattr(delivery_containment, "_probe_channel_mirror", lambda *_a: cell[0])
    return cell


def _user_row(slot) -> dict:
    return next(m for m in slot.messages if m.get("role") == "user")


# ── the marker itself ──


class TestMarker:
    def test_a_body_without_the_key_is_not_marked(self):
        assert agent_origin.send_is_agent_origin({"message": "hi"}) is False
        assert agent_origin.send_is_agent_origin(None) is False

    @pytest.mark.parametrize("value", _MARKS)
    def test_any_value_under_the_key_is_agent_origin(self, value):
        assert agent_origin.send_is_agent_origin({"turn_origin": value}) is True


# ── a send that starts a turn ──


@pytest.mark.asyncio
class TestStartedTurn:
    @pytest.mark.parametrize(
        ("body", "person"),
        [
            ({"turn_origin": "agent"}, False),
            ({"turn_origin": {"bad": 1}}, False),
            ({}, True),
        ],
    )
    async def test_the_turn_is_the_persons_only_when_unmarked(
        self, tmp_path, monkeypatch, _sel, body, person
    ):
        """The unmarked row is the regression pin: a send without the key keeps
        every person-only grant it has today."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._app = "someapp"  # an app-owned tab: attendance decides its approval window
        nudge = MagicMock()
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: nudge)

        seen, _ = await _send(state, body)

        assert seen is not None and seen["_directive_user_origin"] is person
        # Not filed under `user` in the crew log, so `model.route` and the
        # linked-thread echo do not read it as a person's turn either.
        assert seen.get("_turn_actor") == (None if person else "crew")
        assert (_user_row(slot).get("meta") or {}).get("turnActor") == (None if person else "crew")
        # Approvals: only a person's send restores the attended approval window.
        assert slot._human_seen is person
        assert slot.unattended is (not person)
        # The row's human-turn mark.
        assert (HUMAN_TURN_META_KEY in (_user_row(slot).get("meta") or {})) is person
        # Autonudge "user wins".
        assert nudge.notify_user_input.call_args.kwargs["human"] is person


# A GitHub token with a valid checksum; an AWS key ID and a bare 40-character run are
# kept visible, so neither can stand in.
_SECRET = github_classic(seed=52)


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "person"), [({"turn_origin": "agent"}, False), ({}, True)])
async def test_a_marked_send_is_redacted_like_a_drained_non_user_entry(
    tmp_path, monkeypatch, _sel, body, person
):
    """A ``user`` row is served unredacted, which is right only for the person's
    own words. A marked send gets the drain's non-user redaction on the row and
    the turn input alike; the person's own send stays as typed."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    seen_msg: list[str] = []

    async def fake_run_chat(st, sl, msg, **kwargs):
        seen_msg.append(msg)
        sl.append("chunk", "ok", "chunk")

    text = f"use key {_SECRET} now"
    slot._titled = True
    with patch("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "s1", "message": text, **body}, timeout=None
            )
            async for _chunk in resp.content.iter_any():
                break
            resp.close()
            await async_wait_until(lambda: bool(seen_msg), describe=lambda: "no turn")

    assert (_SECRET in _user_row(slot)["content"]) is person
    assert (_SECRET in seen_msg[0]) is person


# ── a send to a busy slot: queue, steer, sub-agent hold ──


def _busy_slot(state):
    slot = state.get_or_create_slot("s1")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=True)
    slot._acp_client = client
    return slot


@pytest.mark.asyncio
class TestBusySlot:
    @pytest.mark.parametrize(("body", "person"), [({"turn_origin": "agent"}, False), ({}, True)])
    async def test_a_queued_send_keeps_its_origin(self, tmp_path, monkeypatch, _sel, body, person):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)

        _, resp = await _send(state, body)

        assert resp["queued"] is True
        assert slot._queue[-1].get("_directive_user_origin", False) is person
        assert (slot._queue[-1].get("meta") or {}).get("turnActor") == (None if person else "crew")

    @pytest.mark.parametrize(
        ("body", "person"),
        [
            ({"turn_origin": "agent"}, False),
            ({"turn_origin": 7}, False),
            ({}, True),
        ],
    )
    async def test_a_marked_steer_stays_out_of_a_turn_a_person_is_driving(
        self, tmp_path, monkeypatch, _sel, body, person
    ):
        """A steer would put the agent's text inside the person's running turn,
        whose ``_directive_user_origin`` its directives keep reading; a marked
        send queues instead, without the act-now intent, and says why."""
        from kiro_crew.dashboard.chat_delivery import COMPOSER_SEND_META

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        slot._turn_user_sent = True  # the running turn is one the person sent

        _, resp = await _send(state, {"steer": True, **body})

        assert (resp.get("steered") is True) is person
        assert slot._acp_client.steer.await_count == (1 if person else 0)
        assert slot._turn_user_sent is True
        if not person:
            assert resp["queued"] is True and resp["heldForPerson"] is True
            assert "steerRefusal" not in resp
            entry = slot._queue[-1]
            assert entry.get("_directive_user_origin", False) is False
            assert not (entry.get("meta") or {}).get(COMPOSER_SEND_META)

    @pytest.mark.parametrize("standing", ["_turn_user_sent", "_turn_owner_sent"])
    async def test_either_standing_of_the_person_keeps_a_marked_steer_out(
        self, tmp_path, monkeypatch, _sel, standing
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        slot._turn_user_sent = slot._turn_owner_sent = False
        setattr(slot, standing, True)

        _, resp = await _send(state, {"steer": True, "turn_origin": "agent"})

        assert resp.get("queued") is True and resp.get("heldForPerson") is True
        assert slot._acp_client.steer.await_count == 0

    async def test_a_marked_steer_reaches_a_turn_no_person_is_driving(
        self, tmp_path, monkeypatch, _sel
    ):
        """A worker's turn is started by its conductor's own send. The conductor's
        steer reaches it, as its steer to a worker on its own crew does, and goes
        in as a peer's text: the row is not the composer's."""
        from kiro_crew.dashboard.chat_delivery import COMPOSER_SEND_META

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        slot._turn_user_sent = slot._turn_owner_sent = False

        _, resp = await _send(state, {"steer": True, "turn_origin": "agent"})

        assert resp == {"ok": True, "steered": True}
        assert slot._acp_client.steer.await_count == 1
        assert slot._turn_user_sent is False and slot._turn_owner_sent is False
        assert slot._queue == []
        rows = [m for m in slot.messages if "hi" in str(m.get("content", ""))]
        assert rows and not any(
            (m.get("meta") or {}).get(COMPOSER_SEND_META)
            or HUMAN_TURN_META_KEY in (m.get("meta") or {})
            for m in rows
        )

    async def test_a_marked_steer_the_agent_holds_behind_a_tool_call_says_so(
        self, tmp_path, monkeypatch, _sel
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        slot._turn_user_sent = slot._turn_owner_sent = False
        slot._acp_client.steer_held_behind_tool = True

        _, resp = await _send(state, {"steer": True, "turn_origin": "agent"})

        assert resp == {"ok": True, "steered": True, "held": True}

    async def test_a_marked_steer_the_turn_refuses_queues_with_the_reason(
        self, tmp_path, monkeypatch, _sel
    ):
        """A refused steer is not lost and not reported as steered: it queues as a
        turn of its own, and the reply names why it did not go in."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        slot._turn_user_sent = slot._turn_owner_sent = False
        slot._acp_client.supports_steer = False

        _, resp = await _send(state, {"steer": True, "turn_origin": "agent"})

        assert resp.get("queued") is True and "steered" not in resp
        assert resp.get("steerRefusal") == "no_steer"
        assert slot._acp_client.steer.await_count == 0
        entry = slot._queue[-1]
        assert entry.get("_directive_user_origin", False) is False
        assert (entry.get("meta") or {}).get("steerRefusal") == "no_steer"

    @pytest.mark.parametrize(
        ("body", "outcome"),
        [
            ({"steer": True, "turn_origin": "agent"}, "starts"),
            ({"steer": True, "turn_origin": "agent"}, "drops"),
            ({"steer": True, "turn_origin": "agent"}, "still busy"),
            ({}, "starts"),
        ],
    )
    async def test_a_queued_send_is_started_only_when_the_drain_ran_it(
        self, tmp_path, monkeypatch, _sel, body, outcome
    ):
        """The turn a send waited behind can end before the route's own drain runs.
        That drain then starts the entry, and an agent's reply says ``started``
        beside ``queued``. An entry the drain drops instead is gone from the queue
        with no turn on it: the reply stays ``queued``. A person's reply never
        carries the field."""
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        slot._turn_user_sent = slot._turn_owner_sent = False
        slot._acp_client.supports_steer = False
        mirror = _mirror_store(monkeypatch)
        real_drain = chat_runner._drain_parked_queues

        async def _turn_ends_then_drain(st, keys, **kwargs):
            if outcome != "still busy":
                slot.task = None
            if outcome == "drops":
                mirror[0] = None
            await real_drain(st, keys, **kwargs)

        monkeypatch.setattr(chat_runner, "_drain_parked_queues", _turn_ends_then_drain)

        resp, ran = await _send_through_the_drain(state, body)

        assert resp.get("queued") is True and "steered" not in resp
        if outcome == "starts" and body.get("turn_origin") == "agent":
            assert resp["started"] is True
        else:
            assert "started" not in resp
        assert ran == (["hi"] if outcome == "starts" else [])
        assert any(i.get("id") == resp["queue_id"] for i in slot._queue) is (
            outcome == "still busy"
        )

    async def test_a_steer_queued_on_a_replaced_chat_is_refused(self, tmp_path, monkeypatch, _sel):
        from kiro_crew.dashboard import agent_steer

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        slot._turn_user_sent = slot._turn_owner_sent = False

        async def _steer(st, sl, message):
            return None, agent_steer.TARGET_MOVED, False

        monkeypatch.setattr(agent_steer, "steer", _steer)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"slot": "s1", "message": "hi", "steer": True, "turn_origin": "agent"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "target_moved"
        assert slot._queue == []

    @pytest.mark.parametrize(("body", "person"), [({"turn_origin": "agent"}, False), ({}, True)])
    async def test_an_auto_steer_asks_jev_only_for_a_person(
        self, tmp_path, monkeypatch, _sel, body, person
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        slot._turn_user_sent = True
        decide = AsyncMock(return_value=(False, None))
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.decided_message_handling", decide)

        _, resp = await _send(state, {"steer": "auto", **body})

        assert decide.await_count == (1 if person else 0)
        assert (resp.get("steered") is True) is person

    @pytest.mark.parametrize(("body", "person"), [({"turn_origin": "agent"}, False), ({}, True)])
    async def test_a_send_held_behind_sub_agents_keeps_its_origin(
        self, tmp_path, monkeypatch, _sel, body, person
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.subagents_hold_user_messages",
            lambda *_a: True,
        )

        seen, resp = await _send(state, body)

        if person:
            # The session's own human is never held behind sub-agents: the turn
            # starts now, with the person's directive origin.
            assert resp is None
            assert seen["_directive_user_origin"] is True
            return
        assert resp["queued"] is True
        assert slot._queue[-1].get("_directive_user_origin", False) is False
        assert (slot._queue[-1].get("meta") or {}).get("turnActor") == "crew"

    @pytest.mark.parametrize("drops", [False, True])
    async def test_a_held_send_is_started_only_when_the_drain_ran_it(
        self, tmp_path, monkeypatch, _sel, drops
    ):
        """An idle worker's sub-agent hold can lift while the send is admitted. The
        route's own drain then starts the entry, and the reply says so beside
        ``queued``, so the hub's receipt reads started. When the session store
        stops answering at that moment, the drain's sweep drops the entry instead:
        it is gone from the queue, no turn ran, and the reply stays ``queued``."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        mirror = _mirror_store(monkeypatch)
        holds = iter([True])  # held at the gate, lifted when the route drains

        def _hold(*_a):
            held = next(holds, False)
            if not held and drops:
                mirror[0] = None
            return held

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.subagents_hold_user_messages", _hold)

        resp, ran = await _send_through_the_drain(state, {"steer": True, "turn_origin": "agent"})

        assert resp.get("queued") is True and "steered" not in resp
        assert not any(i.get("id") == resp["queue_id"] for i in slot._queue)
        if drops:
            assert "started" not in resp
            assert ran == []
            assert any(m.get("role") == "notice" for m in slot.messages)
        else:
            assert resp["started"] is True
            assert ran == ["hi"]

    @pytest.mark.parametrize(("body", "held"), [({"turn_origin": "agent"}, True), ({}, False)])
    async def test_only_a_persons_steer_skips_the_sub_agent_hold(
        self, tmp_path, monkeypatch, _sel, body, held
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.get_or_create_slot("s1")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.subagents_hold_user_messages",
            lambda *_a: True,
        )

        seen, resp = await _send(state, {"steer": True, **body})

        assert (resp is not None and resp.get("queued") is True) is held
        assert (seen is not None) is (not held)


# ── the consumers: what the turn's provenance admits ──


async def _turn_user_sent_for(tmp_path, body: dict) -> bool:
    """Send *body*, then run the REAL turn with the kwargs the handler passed and
    read the ``_turn_user_sent`` the turn records for its duration."""
    from test_acp_tool_identity import _stub_state

    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
    from kiro_crew.dashboard import chat_runner as cr

    send_state = _make_state(tmp_path)
    send_state.get_or_create_slot("s1")
    kwargs, _ = await _send(send_state, body)
    assert kwargs is not None

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    seen: list[bool] = []

    async def _stream(_msg):
        seen.append(slot._turn_user_sent)
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
        yield AcpEvent(kind=EVENT_COMPLETE)

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    await cr._run_chat(state, slot, "hi", **kwargs)
    (value,) = seen
    return value


def _guide_call(user_sent: bool, method_path: str, payload: dict, extra=()) -> tuple[int, dict]:
    state = FakeState()
    state.open_slot("chat-1")._turn_user_sent = user_sent

    async def go(client):
        resp = await client.post(method_path, json=payload, headers=agent("dashboard:chat-1"))
        return resp.status, await resp.json()

    return run_guide_app(go, state, extra_routes=extra)


def _start_guide(user_sent: bool) -> tuple[int, dict]:
    return _guide_call(user_sent, "/api/guide/agent/start", {"actions": CREWMATE})


def _propose_card(user_sent: bool) -> tuple[int, dict]:
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    propose = ("POST", "/api/cards/agent/propose", card_routes.api_cards_agent_propose)
    return _guide_call(
        user_sent,
        "/api/cards/agent/propose",
        {"kind": "chat.rename", "params": {"title": "x"}},
        extra=[propose],
    )


def _update_self(user_sent: bool) -> tuple[int, dict]:
    from kiro_crew.dashboard.handlers import member_self as self_routes

    route = ("POST", "/api/member/self/update", self_routes.api_member_self_update)
    return _guide_call(
        user_sent,
        "/api/member/self/update",
        {"fields": {"display_name": "Pebble"}},
        extra=[route],
    )


@pytest.mark.parametrize("action", [_start_guide, _propose_card, _update_self])
class TestGuideAndCardActions:
    def test_an_agent_origin_turn_is_refused(self, tmp_path, monkeypatch, _sel, action):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        user_sent = asyncio.run(_turn_user_sent_for(tmp_path, {"turn_origin": "agent"}))
        assert user_sent is False
        status, body = action(user_sent)
        assert (status, body["code"]) == (403, "not_user_turn")

    def test_the_same_send_unmarked_is_admitted(self, tmp_path, monkeypatch, _sel, action):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        user_sent = asyncio.run(_turn_user_sent_for(tmp_path, {}))
        assert user_sent is True
        _status, body = action(user_sent)
        assert body.get("code") != "not_user_turn"


@pytest.mark.asyncio
class TestUserOriginDirectives:
    """``suggest_followup`` posts a card only from a turn a person sent; the turn's
    ``_directive_user_origin`` is what ``producer_is_user_facing`` carries."""

    @pytest.mark.parametrize(("body", "person"), [({"turn_origin": "agent"}, False), ({}, True)])
    async def test_suggest_followup_follows_the_send(
        self, tmp_path, monkeypatch, _sel, body, person
    ):
        from kiro_crew.dashboard.session_directive_apply import apply_session_directive

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        kwargs, _ = await _send(state, body)
        assert kwargs is not None

        slot = MagicMock()
        slot.key = "dashboard:s1"
        result = await apply_session_directive(
            None,
            slot,
            "dashboard:s1",
            "suggest_followup",
            {"suggestions": [{"label": "x", "prompt": "do x"}]},
            producer_is_user_facing=kwargs["_directive_user_origin"],
        )
        assert ("a turn a person sent" in str(result)) is (not person)
