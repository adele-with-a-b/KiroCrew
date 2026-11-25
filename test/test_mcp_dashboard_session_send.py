"""`session_send`'s tool layer: what it forwards, and what it reports back.

Three delivery outcomes reach this layer from the API (`steered`, `started`,
neither), and a caller coordinating several sessions acts on the difference. A
steer that quietly fell back to the queue while the report says "queued" reads as
"the target was busy" — true, but not the thing that happened — so each outcome is
asserted on its own.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext
from kiro_crew.validation import SESSION_SEND_SCHEMA, ValidationError, validate_tool_args


def _send(args: dict, resp: dict) -> tuple[str, DashboardRequest]:
    """One ``session_send`` frame; the reply text and the request the route got."""
    dash = InMemoryDashboardClient({"POST /api/session-control/send": resp})
    out = TABLE.call("session_send", args, ToolContext(dash, Caller.strict("dashboard:chat-1-100")))
    (request,) = dash.requests
    return out, request


class TestSteerForwarding:
    def test_steer_is_forwarded_to_the_api(self) -> None:
        _, sent = _send(
            {"target": "chat-2", "message": "stop that", "steer": True},
            {"ok": True, "target": "chat-2", "started": False, "steered": True},
        )
        assert sent.path == "/api/session-control/send"
        # ``receipt`` asks the gateway for this call's outgoing-card record.
        assert sent.body == {
            "target": "chat-2",
            "message": "stop that",
            "steer": True,
            "receipt": True,
        }
        assert sent.session_key == "dashboard:chat-1-100"

    def test_omitting_steer_forwards_nothing_so_the_gateway_picks(self) -> None:
        """A missing key and a false key are two cases: absent lets the gateway
        steer the caller's own worker, false asks for the queue by name."""
        _, sent = _send(
            {"target": "chat-2", "message": "later is fine"},
            {"ok": True, "target": "chat-2", "started": False, "steered": False},
        )
        assert "steer" not in sent.body

    def test_an_explicit_false_is_forwarded(self) -> None:
        _, sent = _send(
            {"target": "chat-2", "message": "after this turn", "steer": False},
            {"ok": True, "target": "chat-2", "started": False, "steered": False},
        )
        assert sent.body["steer"] is False


class TestOutcomeReports:
    def test_a_steered_delivery_says_it_cut_into_the_running_turn(self) -> None:
        out, _ = _send(
            {"target": "chat-2", "message": "stop that", "steer": True},
            {"ok": True, "target": "chat-2", "started": False, "steered": True},
        )
        assert "Steered" in out and "chat-2" in out
        assert "Queued" not in out

    def test_a_steer_held_behind_a_tool_call_is_not_reported_as_read(self) -> None:
        """The agent took the steer and keeps it until the tool call it is in
        returns, which can be minutes: the receipt must not say "next step"."""
        out, _ = _send(
            {"target": "chat-2", "message": "stop that", "steer": True},
            {"ok": True, "target": "chat-2", "started": False, "steered": True, "held": True},
        )
        assert "Steered" in out
        assert "has not read your message yet" in out and "Nothing was cut short" in out
        assert "at its next step" not in out
        plain, _ = _send(
            {"target": "chat-2", "message": "stop that", "steer": True},
            {"ok": True, "target": "chat-2", "started": False, "steered": True},
        )
        assert "at its next step" in plain and "has not read" not in plain

    def test_a_started_turn_is_reported_as_delivered(self) -> None:
        out, _ = _send(
            {"target": "chat-2", "message": "pick this up", "steer": True},
            {"ok": True, "target": "chat-2", "started": True, "steered": False},
        )
        assert "started a turn" in out

    def test_a_steer_that_fell_back_to_the_queue_says_so(self) -> None:
        """The caller asked for an interruption and did not get one. Reporting a
        plain queue would leave it believing the target was interrupted."""
        out, _ = _send(
            {"target": "chat-2", "message": "stop that", "steer": True},
            {"ok": True, "target": "chat-2", "started": False, "steered": False},
        )
        assert "Queued" in out
        assert "could not go into the running turn" in out

    def test_a_steer_that_fell_back_names_why_and_what_it_waits_for(self) -> None:
        """A turn the agent started on its own can run for hours, so the sender is
        told that, not only that its steer was queued."""
        out, _ = _send(
            {"target": "chat-2", "message": "rebase first", "steer": True},
            {
                "ok": True,
                "target": "chat-2",
                "started": False,
                "steered": False,
                "steerRefusal": "agent_turn",
            },
        )
        assert "Queued" in out
        assert "the agent started this turn on its own" in out
        assert "can be a long while" in out

    def test_an_unknown_refusal_code_reads_as_the_plain_fallback(self) -> None:
        out, _ = _send(
            {"target": "chat-2", "message": "stop", "steer": True},
            {
                "ok": True,
                "target": "chat-2",
                "started": False,
                "steered": False,
                "steerRefusal": "x",
            },
        )
        assert "could not go into the running turn, so it was queued instead" in out

    def test_a_plain_queued_delivery_does_not_mention_steering(self) -> None:
        out, _ = _send(
            {"target": "chat-2", "message": "after this turn"},
            {"ok": True, "target": "chat-2", "started": False, "steered": False},
        )
        assert "Queued" in out
        assert "steer" not in out.lower()

    def test_a_default_steer_names_the_word_that_queues(self) -> None:
        """The caller asked for nothing and its own worker was steered: the reply
        says so, so a caller that wanted the queue learns how to ask for it."""
        out, _ = _send(
            {"target": "chat-2", "message": "the answer is yes"},
            {
                "ok": True,
                "target": "chat-2",
                "started": False,
                "steered": True,
                "steerDefault": "manager",
            },
        )
        assert "Steered" in out and "steer=false" in out

    def test_a_default_steer_that_fell_back_names_why(self) -> None:
        out, _ = _send(
            {"target": "chat-2", "message": "the answer is yes"},
            {
                "ok": True,
                "target": "chat-2",
                "started": False,
                "steered": False,
                "steerDefault": "manager",
                "steerRefusal": "permission_wait",
            },
        )
        assert "Queued" in out and "waiting on a tool approval" in out


class TestSchema:
    def test_a_non_boolean_steer_is_refused_at_the_schema(self) -> None:
        """The model supplies these arguments, so a string "true" is a real input
        shape. Coercing it would steer on a value the caller never meant as one."""
        with pytest.raises(ValidationError):
            validate_tool_args(
                {"target": "chat-2", "message": "hi", "steer": "true"}, SESSION_SEND_SCHEMA
            )

    def test_steer_has_no_default(self) -> None:
        cleaned = validate_tool_args({"target": "chat-2", "message": "hi"}, SESSION_SEND_SCHEMA)
        assert cleaned.get("steer") is None
