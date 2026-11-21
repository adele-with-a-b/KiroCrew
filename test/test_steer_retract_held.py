"""Taking back a claude steer the client still holds (``ACP_BACKENDS_STEER_HELD_RETRACT``).

claude-agent-acp drops no single unread message: ``$/cancel_request`` naming a
queued prompt changes nothing, and ``session/cancel`` takes the running turn with
it (``test/fixtures/acp_frames/claude/queued-prompt-*-live.jsonl``). What can be
taken back is a steer ``AcpClient`` holds behind an open tool call, until it is
sent. These tests drive the dashboard side with a client stub that holds steers;
the client's own hold is pinned in ``test_claude_steering_request.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import steer_requeue_helpers
from aiohttp.test_utils import TestClient, TestServer
from test_steer_retract import _frames, _mid_of, _retract, _steer, _steer_rows, harness

from kiro_crew.dashboard import steer_retract
from kiro_crew.dashboard.chat_delivery import (
    STEER_RETRACTABLE_META,
    STEER_STATE_RETRACTED,
    STEER_STATE_WRITTEN,
)

_patch_sel = steer_requeue_helpers._patch_sel

__all__ = ["harness"]


class _HoldingClient:
    """claude's client as the take-back sees it: steers sent while ``tool_open``
    are held, and a held one can be dropped until it is released."""

    supports_steer = True
    supports_steer_retract = True
    takes_back_held_steers = True

    def __init__(self) -> None:
        self.tool_open = True
        self.held: list[str] = []
        self.sent: list[str] = []
        self.clear_pending_steers = AsyncMock(return_value=True)

    async def steer(self, text: str) -> bool:
        (self.held if self.tool_open else self.sent).append(text)
        return True

    def steer_is_held(self, text: str) -> bool:
        return text in self.held

    def drop_held_steer(self, text: str) -> bool:
        if text not in self.held:
            return False
        self.held.remove(text)
        return True

    def release(self) -> list[str]:
        released, self.held = self.held, []
        self.sent.extend(released)
        return released


@pytest.fixture
def holding(harness):
    harness.slot._acp_client = _HoldingClient()
    return harness


class TestAHeldSteer:
    """claude: a steer is taken back while the client holds it behind a tool call."""

    @pytest.mark.asyncio
    async def test_only_a_held_steer_is_offered(self, holding):
        client = holding.slot._acp_client
        async with TestClient(TestServer(holding.app)) as http:
            await _steer(http, "held behind the call")
            client.tool_open = False
            await _steer(http, "sent at once")

        offers = {
            r["content"]: r["meta"].get(STEER_RETRACTABLE_META) for r in _steer_rows(holding.slot)
        }
        assert offers == {"held behind the call": True, "sent at once": None}
        assert [p.get(STEER_RETRACTABLE_META) for p in _frames(holding.state, "steer_push")] == [
            True,
            None,
        ]

    @pytest.mark.asyncio
    async def test_unsend_drops_that_steer_and_settles_before_the_route_answers(self, holding):
        client = holding.slot._acp_client
        async with TestClient(TestServer(holding.app)) as http:
            for text in ("first", "take me back", "last"):
                await _steer(http, text)
            resp = await _retract(http, _mid_of(holding.slot, "take me back"), "unsend")
            assert resp.status == 202

        client.clear_pending_steers.assert_not_awaited()
        assert client.held == ["first", "last"], "only that steer leaves the client"
        assert holding.slot._pending_steers == ["first", "last"]
        assert holding.slot._steer_retract is None
        assert holding.slot._steer_clear is None
        states = {r["meta"]["mid"]: r["meta"]["steerState"] for r in _steer_rows(holding.slot)}
        assert list(states.values()).count(STEER_STATE_RETRACTED) == 1
        (taken,) = [
            r for r in _steer_rows(holding.slot) if r["meta"]["steerState"] == STEER_STATE_RETRACTED
        ]
        assert taken["content"] == ""
        assert taken["meta"][steer_retract.RETRACT_ACTION_META] == "unsend"
        assert client.release() == ["first", "last"]

    @pytest.mark.asyncio
    async def test_queue_instead_queues_the_held_steer(self, holding):
        async with TestClient(TestServer(holding.app)) as http:
            await _steer(http, "run me next")
            resp = await _retract(http, _mid_of(holding.slot, "run me next"), "queue")
            assert resp.status == 202

        (entry,) = holding.slot._queue
        assert entry["content"] == "run me next"
        assert holding.slot._pending_steers == []
        assert holding.slot._acp_client.held == []

    @pytest.mark.asyncio
    async def test_a_steer_already_released_is_not_pending_and_stops_offering(self, holding):
        client = holding.slot._acp_client
        async with TestClient(TestServer(holding.app)) as http:
            await _steer(http, "on its way")
            mid = _mid_of(holding.slot, "on its way")
            client.release()
            resp = await _retract(http, mid, "edit")

            assert resp.status == 409
            assert (await resp.json())["code"] == "steer_not_pending"
        (row,) = _steer_rows(holding.slot)
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert row["content"] == "on its way"
        assert row["meta"][STEER_RETRACTABLE_META] is False
        assert row["meta"][steer_retract.RETRACT_RELEASED_META] is True
        assert holding.slot._pending_steers == ["on its way"], "the turn reads it"

    @pytest.mark.asyncio
    async def test_the_release_withdraws_the_offer(self, holding):
        client = holding.slot._acp_client
        async with TestClient(TestServer(holding.app)) as http:
            await _steer(http, "on its way")
        mid = _mid_of(holding.slot, "on its way")
        holding.state.broadcast_ws.reset_mock()

        for text in client.release():
            steer_retract.on_released(holding.state, holding.slot, text)
        steer_retract.on_released(holding.state, holding.slot, "on its way")
        steer_retract.on_released(holding.state, holding.slot, "never steered")

        (row,) = _steer_rows(holding.slot)
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert row["meta"][STEER_RETRACTABLE_META] is False
        assert row["meta"][steer_retract.RETRACT_RELEASED_META] is True
        (patch,) = _frames(holding.state, "chat_message_update")
        assert patch["mid"] == mid, "one patch: a row no longer offering is left alone"

    def test_a_release_leaves_a_row_that_was_never_offered(self, holding):
        holding.slot.append(
            "user",
            "sent at once",
            "msg msg-u",
            meta={"steer": True, "steerState": STEER_STATE_WRITTEN},
        )
        steer_retract.on_released(holding.state, holding.slot, "sent at once")
        assert _frames(holding.state, "chat_message_update") == []


def test_the_offer_reads_the_clients_own_answer():
    from kiro_crew.dashboard.chat_delivery import steer_take_back_offered

    kiro = MagicMock(supports_steer_retract=True)
    assert steer_take_back_offered(kiro, "x") is True, "kiro-cli drops any steer it holds"
    assert steer_take_back_offered(MagicMock(supports_steer_retract=False), "x") is False
    held = _HoldingClient()
    assert steer_take_back_offered(held, "x") is False
    held.held.append("x")
    assert steer_take_back_offered(held, "x") is True


@pytest.mark.asyncio
async def test_the_event_loop_hands_a_release_to_the_take_back(monkeypatch):
    from turn_harness import TurnScript, run_turn

    from kiro_crew.acp.types import EVENT_STEER_RELEASED, EVENT_TEXT_CHUNK, AcpEvent

    handed: list[str] = []
    monkeypatch.setattr(
        steer_retract, "on_released", lambda _state, _slot, text: handed.append(text)
    )

    await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_STEER_RELEASED, text="on its way"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply"),
            ]
        )
    )

    assert handed == ["on its way"]
