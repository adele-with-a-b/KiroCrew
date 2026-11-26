"""A sleeping `wait` ends when a message is queued for its chat, and says who waits.

A queued message (a worker's note, a cron card, the owner's message his turn
refused) runs only once the turn ends, and one sleep can last 1800 s, so the
sleep ends and the agent is told who waits. These drive the real `wait` tool's
steps against the real keepalive handler and a real chat slot: each tool ping is
the handler call it would be over HTTP, and the tool's clock is a fake the test
advances.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_core
from kiro_crew.dashboard import queue_age
from kiro_crew.dashboard.chat_utils_injected_rows import (
    AUTOMATION_STEER_META,
    CRON_NOTIFICATION_KIND,
)
from kiro_crew.dashboard.handlers.sessions import _service_wait_ping, api_session_keepalive
from kiro_crew.dashboard.session_control_parts.delivery_containment import SEND_ORIGIN_META_KEY
from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.mcp_tools.control import _queued_data, _WaitStep

SLOT = "chat-1"
SECONDS = 600


class _ToolClock:
    """The wait tool's monotonic clock, moved only by the test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.get_slot = MagicMock(side_effect=lambda name: state._slots.get(name))
    state.push_slots_update = MagicMock()
    return state


class _Sleep:
    """One `wait` sleep whose pings reach the real handler for *slot*."""

    def __init__(self, slot: _ChatSlot) -> None:
        self.slot = slot
        self.state = _state(slot)
        self.clock = _ToolClock()
        self.provider = SimpleNamespace(last_steer_monotonic=0.0)
        self._time = SimpleNamespace(monotonic=self.clock.monotonic, time=time.time)
        with patch.object(mcp_core, "time", self._time):
            self.step = _WaitStep(seconds=SECONDS, reason_safe="CI run", identified=True)

    def _post(self, path: str, body: dict | None = None, **_kwargs) -> dict:
        reply: dict = {"ok": True}
        body = dict(body or {})
        wait_id = str(body.get("wait_id") or "").strip()[:64]
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value=SLOT):
            _service_wait_ping(self.state, "known", wait_id, body, reply, self.provider)
        return reply

    def tick(self) -> str | None:
        """Run the tool's next ping, then move its clock one ping on."""
        with (
            patch.object(mcp_core, "time", self._time),
            patch.object(mcp_core, "_post", side_effect=self._post),
        ):
            result = self.step.step()
        self.clock.now += mcp_core.WAIT_PING_SECS
        return result


def _peer_meta(sender: str) -> dict:
    return {SEND_ORIGIN_META_KEY: {"slot": sender, "tab": "tab-1"}}


def _queued(result: str) -> dict:
    """The ``queued=`` data a result carries."""
    head = result.split("queued=", 1)[1]
    return json.loads(head[: head.index("}. ") + 1])


class TestAQueuedMessageEndsTheSleep:
    def test_a_queued_owner_message_ends_it_and_is_counted_as_the_owner(self):
        slot = _ChatSlot(SLOT)
        sleep = _Sleep(slot)
        assert sleep.tick() is None  # first ping: the sleep is minted
        slot.queue_append("are you there?", directive_user_origin=True, directive_owner_origin=True)
        result = sleep.tick()
        print(result)

        assert result is not None
        assert result.startswith(f"Wait ended early after 5s of {SECONDS}s: messages are queued")
        assert _queued(result) == {
            "total": 1,
            "owner": 1,
            "people": 0,
            "automation": 0,
            "chats": {},
        }
        assert "are you there?" not in result
        assert result.endswith("Resuming: CI run")
        assert slot._wait_state is None

    def test_a_queued_peer_note_names_the_sending_chat_by_key(self):
        slot = _ChatSlot(SLOT)
        sleep = _Sleep(slot)
        assert sleep.tick() is None
        assert sleep.tick() is None
        slot.queue_append("report: tests pass", meta=_peer_meta("dashboard_chat-7"))
        slot.queue_append("[Cron notification]", kind=CRON_NOTIFICATION_KIND)
        result = sleep.tick()
        print(result)

        assert result is not None
        assert _queued(result) == {
            "total": 2,
            "owner": 0,
            "people": 0,
            "automation": 1,
            "chats": {"chat-7": 1},
        }
        assert "report: tests pass" not in result

    def test_a_message_queued_before_the_sleep_does_not_end_it(self):
        """Fires once: the baseline is the queue as it read when this sleep began,
        so a note the turn has not reached yet cannot end sleep after sleep."""
        slot = _ChatSlot(SLOT)
        slot.queue_append("older note", meta=_peer_meta("dashboard_chat-7"))
        sleep = _Sleep(slot)

        assert [sleep.tick() for _ in range(5)] == [None] * 5
        assert slot._wait_state is not None

    def test_a_queued_wake_does_not_end_it(self):
        """A wake found in the queue is one a turn ended without reading, and its
        items may have closed since; a live wake comes in as a steer instead."""
        slot = _ChatSlot(SLOT)
        sleep = _Sleep(slot)
        assert sleep.tick() is None
        slot.queue_append(
            "[work-ledger wake] item=it_1 status=done",
            meta={AUTOMATION_STEER_META: {"cycle": 3, "loop_id": "loop-1"}},
        )
        results = [sleep.tick() for _ in range(5)]
        print(results)

        assert results == [None] * 5
        assert slot._wait_state is not None

    def test_a_restored_card_that_waits_for_a_person_does_not_end_it(self):
        slot = _ChatSlot(SLOT)
        sleep = _Sleep(slot)
        assert sleep.tick() is None
        qid = slot.queue_append("restored", meta=_peer_meta("dashboard_chat-7"))
        next(e for e in slot._queue if e["id"] == qid)[RESTORED_QUEUE_KEY] = True

        assert [sleep.tick() for _ in range(3)] == [None] * 3

    def test_the_sleep_runs_out_with_nothing_queued(self):
        slot = _ChatSlot(SLOT)
        sleep = _Sleep(slot)
        results = [sleep.tick() for _ in range(int(SECONDS / mcp_core.WAIT_PING_SECS) + 1)]

        assert results[:-1] == [None] * (len(results) - 1)
        assert results[-1] == f"Waited {SECONDS}s. Resuming: CI run"

    def test_a_steer_still_reads_as_a_message_in_the_turn(self):
        """A steer and a queued note on the same ping: the steer is already in the
        turn, so the steer's result wins."""
        slot = _ChatSlot(SLOT)
        sleep = _Sleep(slot)
        assert sleep.tick() is None
        slot.queue_append("note", meta=_peer_meta("dashboard_chat-7"))
        sleep.provider.last_steer_monotonic = 5.0
        result = sleep.tick()

        assert result is not None
        assert "a message came into this turn" in result


class TestTheWhoWaitsData:
    def test_past_eight_chats_the_rest_are_summed_not_named(self):
        slot = _ChatSlot(SLOT)
        for n in range(10):
            slot.queue_append(f"note {n}", meta=_peer_meta(f"dashboard_chat-{n}"))

        data = queue_age.senders(slot).as_data()

        assert data["total"] == 10
        assert list(data["chats"]) == [f"chat-{n}" for n in range(8)]
        assert data["chats_more"] == 2

    def test_the_tool_keeps_only_counts_and_bounded_keys_from_the_reply(self):
        raw = {
            "total": 2,
            "owner": True,
            "people": -1,
            "automation": 1,
            "text": "the note itself",
            "chats": {"chat-1": 1, "x" * 300: 1, "chat-2": "1"},
        }

        assert _queued_data(raw) == {
            "total": 2,
            "automation": 1,
            "chats": {"chat-1": 1, "x" * 128: 1},
        }
        assert _queued_data("not a dict") == {}


def _keepalive_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/session-keepalive", api_session_keepalive)
    return app


class TestASubAgentCollectionIsNotEnded:
    @pytest.mark.asyncio
    async def test_a_collection_ping_gets_no_end_while_a_message_is_queued(self):
        """``spawn_sub_agents`` pings the same route with no ``wait_id``: cutting
        its collection short would orphan sub-agents still running, so a queued
        message ends nothing there, and the sleep this slot tracks is untouched."""
        slot = _ChatSlot(SLOT)
        state = _state(slot)
        provider = MagicMock()
        state.sessions = SimpleNamespace(get_provider=lambda key: provider)
        state.subagents = None
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value=SLOT):
            _service_wait_ping(
                state, "known", "w1", {"wait_id": "w1", "seconds": 600, "remaining": 600}, {}
            )
        slot.queue_append("note", meta=_peer_meta("dashboard_chat-7"))

        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value=SLOT):
            async with TestClient(TestServer(_keepalive_app(state))) as client:
                resp = await client.post(
                    "/api/session-keepalive",
                    json={"tool_call_id": "", "phase": "poll", "subagent_ids": ["sa-1"]},
                    headers={"X-Session-Key": "known"},
                )
                assert resp.status == 200
                assert await resp.json() == {"ok": True}

        assert slot._wait_state is not None
        assert slot._wait_state["wait_id"] == "w1"
