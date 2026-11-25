"""The MCP half of ``crew=`` (``mcp_session_crew``) and the switch (``remote_dispatch``).

Each tool body is driven with a recording client: what it sends on which route,
and what it renders for a success and for a refusal. The gateway's own checks are
covered by ``test_session_control_crew.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import mcp_session_crew as tools
from kiro_crew import remote_dispatch
from kiro_crew.mcp_tools.dashboard_client import DashboardError


class _Client:
    """Records every call; answers *reply*, or raises *refuse* as a DashboardError."""

    def __init__(self, reply: dict[str, Any] | None = None, refuse: dict | None = None) -> None:
        self.reply = reply or {}
        self.refuse = refuse
        self.calls: list[tuple[str, str, Any]] = []

    def _answer(self) -> dict[str, Any]:
        if self.refuse is not None:
            raise DashboardError(self.refuse)
        return self.reply

    def post(self, path: str, body: dict, *, session_key: str = "") -> dict[str, Any]:
        self.calls.append(("POST", path, body))
        return self._answer()

    def get(self, path: str, *, session_key: str = "") -> dict[str, Any]:
        self.calls.append(("GET", path, None))
        return self._answer()


def _ctx(client: _Client) -> Any:
    return SimpleNamespace(client=client, caller_key="dashboard:chat-1")


_REFUSAL = {"error": "nope", "code": "crew_owner_unverifiable"}


# ── schema overlay ───────────────────────────────────────────────────────────


def test_the_overlay_adds_crew_only_when_enabled_and_never_mutates_its_input():
    rows = [
        {"name": "session_send", "inputSchema": {"properties": {"target": {}}}},
        {"name": "chat_folder_tree", "inputSchema": {"properties": {}}},
    ]
    assert tools.with_crew_schema(rows, False) is rows
    out = tools.with_crew_schema(rows, True)
    assert "crew" in out[0]["inputSchema"]["properties"]
    assert "crew" not in out[1]["inputSchema"]["properties"]
    assert "crew" not in rows[0]["inputSchema"]["properties"]


# ── create ───────────────────────────────────────────────────────────────────


def test_create_sends_only_the_crew_fields_and_renders_the_key():
    client = _Client({"target": "chat-77", "crew_name": "lab", "title": "w", "agent": "worker"})
    out = tools.run_crew_create({"crew": "lab", "title": "w", "agent": "worker"}, _ctx(client))
    assert client.calls == [
        ("POST", "/api/session-control/create", {"crew": "lab", "title": "w", "agent": "worker"})
    ]
    assert "`chat-77`" in out and "crew `lab`" in out and "`worker`" in out


def test_create_reports_a_failed_rename():
    client = _Client({"target": "chat-77", "crew": "inst", "title_error": "rename failed"})
    out = tools.run_crew_create({"crew": "inst"}, _ctx(client))
    assert "title was not set: rename failed" in out


def test_create_refuses_a_local_only_field_before_sending():
    client = _Client()
    out = tools.run_crew_create({"crew": "lab", "folder": "f"}, _ctx(client))
    assert out.startswith("Error:") and "folder" in out
    assert client.calls == []


def test_create_forwards_agent_kind_for_the_gateway_to_judge():
    client = _Client({"target": "chat-77", "crew": "inst"})
    args = {"crew": "lab", "agent": "kirocrew-worker", "agent_kind": "template"}
    tools.run_crew_create(args, _ctx(client))
    assert client.calls == [("POST", "/api/session-control/create", args)]


def test_create_renders_a_refusal():
    out = tools.run_crew_create({"crew": "lab"}, _ctx(_Client(refuse=_REFUSAL)))
    assert out.startswith("Error: could not create a session on that crew: nope")


# ── send ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "steer, reply, phrase",
    [
        (False, {"steered": False, "started": True}, "it was idle and started a turn on it"),
        (False, {"steered": False, "started": False}, "queued: it runs when its current turn"),
        (True, {"steered": True, "started": False}, "steered into its running turn"),
        # An idle session has no turn to steer: the steer that was asked for is
        # not reported as one that failed.
        (True, {"steered": False, "started": True}, "it was idle and started a turn on it"),
        (
            True,
            {"steered": False, "started": False, "steerRefusal": "permission_wait"},
            "queued, not steered",
        ),
        (True, {"steered": False, "started": False}, "queued, not steered"),
    ],
)
def test_send_renders_what_the_crew_did(steer, reply, phrase):
    client = _Client({"target": "chat-77", "crew": "inst", **reply})
    args = {"crew": "lab", "target": "chat-77", "message": "go", "steer": steer}
    out = tools.run_crew_send(args, _ctx(client))
    assert phrase in out
    sent = client.calls[0][2]
    assert sent["message"] == "go" and sent.get("steer", False) is steer


@pytest.mark.parametrize("asked", [{}, {"steer": None}])
def test_an_omitted_steer_cuts_into_the_callers_own_crew_worker(asked):
    """A crew send reaches only a session the caller opened there, so leaving
    ``steer`` out cuts in, as it does for a local session the caller created."""
    client = _Client({"target": "chat-77", "crew": "inst", "steered": True, "started": False})
    args = {"crew": "lab", "target": "chat-77", "message": "go", **asked}
    out = tools.run_crew_send(args, _ctx(client))
    assert client.calls[0][2].get("steer") is True
    assert "steered into its running turn" in out


def test_an_omitted_steer_that_fell_back_names_why():
    from kiro_crew import steer_refusal

    reply = {"target": "chat-77", "crew": "inst", "steered": False, "steerRefusal": "agent_turn"}
    args = {"crew": "lab", "target": "chat-77", "message": "go"}
    out = tools.run_crew_send(args, _ctx(_Client(reply)))
    assert "queued, not steered" in out
    assert steer_refusal.describe("agent_turn") in out


def test_an_omitted_steer_the_crew_merely_queued_is_not_a_failed_steer():
    reply = {"target": "chat-77", "crew": "inst", "steered": False, "started": False}
    args = {"crew": "lab", "target": "chat-77", "message": "go"}
    out = tools.run_crew_send(args, _ctx(_Client(reply)))
    assert "queued: it runs when its current turn ends" in out
    assert "not steered" not in out


def test_an_omitted_steer_into_a_person_driven_turn_says_so():
    reply = {"target": "chat-77", "crew": "inst", "steered": False, "heldForPerson": True}
    args = {"crew": "lab", "target": "chat-77", "message": "go"}
    out = tools.run_crew_send(args, _ctx(_Client(reply)))
    assert "A person is driving that turn" in out


def test_a_defaulted_cut_in_says_how_to_queue_instead():
    reply = {"target": "chat-77", "crew": "inst", "steered": True, "started": False}
    args = {"crew": "lab", "target": "chat-77", "message": "go"}
    out = tools.run_crew_send(args, _ctx(_Client(reply)))
    assert "cuts in unless you pass steer=false" in out
    asked = tools.run_crew_send({**args, "steer": True}, _ctx(_Client(reply)))
    assert "cuts in unless" not in asked


def test_a_steer_held_behind_a_tool_call_is_not_reported_as_read():
    """The crew took the steer, and its agent holds it until the tool call it is in
    returns. The receipt says so instead of implying the worker has read it."""
    reply = {"target": "chat-77", "crew": "inst", "steered": True, "held": True}
    args = {"crew": "lab", "target": "chat-77", "message": "go", "steer": True}
    out = tools.run_crew_send(args, _ctx(_Client(reply)))
    assert "steered into its running turn" in out
    assert "has not read your message yet" in out and "Nothing was cut short" in out
    plain = tools.run_crew_send(args, _ctx(_Client({**reply, "held": False})))
    assert "has not read" not in plain


def test_a_steer_that_fell_back_names_why():
    reply = {
        "target": "chat-77",
        "crew": "inst",
        "steered": False,
        "started": False,
        "steerRefusal": "peer_steer_off",
    }
    args = {"crew": "lab", "target": "chat-77", "message": "go", "steer": True}
    out = tools.run_crew_send(args, _ctx(_Client(reply)))
    assert "queued, not steered" in out
    assert "could not steer its running turn" not in out
    from kiro_crew import steer_refusal

    assert steer_refusal.describe("peer_steer_off") in out


def test_a_steer_kept_out_of_a_persons_turn_says_a_person_is_driving_it():
    reply = {
        "target": "chat-77",
        "crew": "inst",
        "steered": False,
        "started": False,
        "heldForPerson": True,
    }
    args = {"crew": "lab", "target": "chat-77", "message": "go", "steer": True}
    out = tools.run_crew_send(args, _ctx(_Client(reply)))
    assert "queued, not steered" in out and "A person is driving that turn" in out
    assert "could not go into the running turn" not in out


def test_send_renders_a_refusal():
    out = tools.run_crew_send(
        {"crew": "lab", "target": "t", "message": "m"}, _ctx(_Client(refuse=_REFUSAL))
    )
    assert out.startswith("Error: could not send to that crew session")


# ── read ─────────────────────────────────────────────────────────────────────


def test_read_builds_the_query_and_renders_messages():
    client = _Client(
        {
            "target": "chat-77",
            "crew_name": "lab",
            "title": "w",
            "running": True,
            "messages": [{"role": "assistant", "content": "hi"}, "junk"],
        }
    )
    out = tools.run_crew_read({"crew": "lab", "target": "chat-77", "limit": 5}, _ctx(client))
    assert client.calls == [
        ("GET", "/api/session-control/read?target=chat-77&crew=lab&limit=5", None)
    ]
    assert "still working" in out and "[assistant] hi" in out


def test_read_renders_an_empty_transcript():
    client = _Client({"target": "chat-77", "crew": "inst", "messages": []})
    out = tools.run_crew_read({"crew": "lab", "target": "chat-77"}, _ctx(client))
    assert "idle" in out and "No messages yet." in out


def test_read_refuses_since_before_sending():
    client = _Client()
    out = tools.run_crew_read({"crew": "lab", "target": "t", "since": 3}, _ctx(client))
    assert out.startswith("Error:") and client.calls == []


def test_read_renders_a_refusal():
    out = tools.run_crew_read({"crew": "lab", "target": "t"}, _ctx(_Client(refuse=_REFUSAL)))
    assert out.startswith("Error: could not read that crew session")


# ── stop and close ───────────────────────────────────────────────────────────


def test_stop_renders_a_stop_and_a_declined_stop():
    sent = tools.run_crew_stop(
        {"crew": "lab", "target": "t"}, _ctx(_Client({"target": "t", "crew": "i"}))
    )
    assert "Stop sent" in sent
    declined = tools.run_crew_stop(
        {"crew": "lab", "target": "t"},
        _ctx(_Client({"target": "t", "crew": "i", "compacting": True})),
    )
    assert "did not stop it" in declined


def test_stop_and_close_render_a_refusal():
    refusing = _ctx(_Client(refuse=_REFUSAL))
    assert tools.run_crew_stop({"crew": "lab", "target": "t"}, refusing).startswith("Error:")
    assert tools.run_crew_close({"crew": "lab", "target": "t"}, refusing).startswith("Error:")


def test_close_posts_and_renders():
    client = _Client({"target": "t", "crew_name": "lab"})
    out = tools.run_crew_close({"crew": "lab", "target": "t"}, _ctx(client))
    assert client.calls == [("POST", "/api/session-control/close", {"crew": "lab", "target": "t"})]
    assert "Closed `t`" in out


# ── the switch ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value, expected", [(True, True), (False, False)])
def test_the_switch_reads_the_config(monkeypatch, value, expected):
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.work_ledger.remote_dispatch = value
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    assert remote_dispatch.remote_dispatch_enabled() is expected


def test_an_unreadable_config_reads_as_off(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig

    def _boom(cls):
        raise OSError("unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    assert remote_dispatch.remote_dispatch_enabled() is False
