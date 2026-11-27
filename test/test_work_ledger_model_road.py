"""A bound worker's turn that dies because its model account cannot be used wakes its conductor.

A turn can end with no reply at all: the worker's model account is signed out, could
not mint credentials, or had its credentials refused. The chat is left on an error row
and, before this, the item on its last status, so the conductor learned nothing until
the item went stale. Pinned here: the gateway files ``blocked`` with no ``reason``
(the item waits on a service, not on a person), fixed words that start ``Gateway:``
and the gateway-only ``model_road`` artifact; the conductor's ``work-ledger`` probe
wakes on it; one report stands for the whole outage; and a chat with no bound task, a
turn that fails for any other reason, and every retry verdict are unchanged.

The failures are faked frames with made-up names. Reuses the two-conductor fixture of
``test_work_ledger_tools`` and the whole-turn fixture of ``test_dashboard_chat``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_dashboard_chat import TestRunChatTransientRetry as _TransientSuite
from test_work_ledger_tools import (  # noqa: F401 - the autouse fixtures ride along
    CONDUCTOR_A,
    CONDUCTOR_B,
    WORKER_A,
    _clean_slots,
    _isolated_home,
    _open_route,
    _record,
    _report,
    two_by_two,
)
from test_work_ledger_worker_waits import _conductor_wakes, _events, _item, _reports, _waits

from kiro_crew import credential_errors as ce
from kiro_crew import llm_helpers
from kiro_crew import work_ledger as wl
from kiro_crew.acp.client import AcpAuthRequired, AcpError
from kiro_crew.acp.transport_errors import _raise_acp_error
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.llm_helpers import TRANSIENT_RETRIES

#: What a proxy in front of the model answers when the account's credential helper
#: fails, as one harness frames it: the 5xx status sits beside the provider's name.
_UNMINTED = (
    "CredentialRetrievalError: Error when retrieving credentials from custom-process: "
    "helper exited with status 1"
)
PROMPT_FRAME = {
    "code": -32603,
    "message": f"Internal error: API Error: 502 proxy.example.test: {_UNMINTED}",
}
#: The same failure from a harness that answers it on a request other than the
#: prompt: the client raises the frame verbatim, with no curated wording.
REQUEST_FRAME_TEXT = (
    "JSON-RPC error: {'code': -32603, 'message': 'Internal error', 'data': {'details': "
    f"'API error: 502 proxy.example.test: RuntimeError: account demo: {_UNMINTED}'}}"
)
#: A provider hiccup with a working account: the same 5xx wording, no credential name.
PLAIN_5XX_FRAME = {
    "code": -32603,
    "message": "Internal error: API Error: 502 proxy.example.test: upstream reset",
}


@pytest.fixture
def a_second_per_stamp(monkeypatch):
    """The ledger's one clock (``_now_iso``) ticks a second per stamp.

    Stamps are seconds-precision and an event id is its stamp plus its fields, so two
    reports with the same words in one second read back as one event. A turn that dies,
    a conductor that answers and a turn that dies again are seconds apart in real use;
    here the clock says so, whichever second the host is in. It starts at the host's
    own second, so no report reads as stale.
    """
    start = datetime.now().astimezone().replace(microsecond=0)
    ticks = iter(range(100_000))

    def _tick() -> str:
        return (start + timedelta(seconds=next(ticks))).isoformat(timespec="seconds")

    monkeypatch.setattr(wl, "_now_iso", _tick)


#: The artifact key and cause values as a conductor reads them off the ledger. The
#: whole-turn tests spell them out, so they fail on the item's state, not on a name.
ARTIFACT = "model_road"
UNAVAILABLE = "credentials_unavailable"
SIGNED_OUT = "signed_out"


def model_road_down_cause(exc: BaseException) -> str:
    return llm_helpers.model_road_down_cause(exc)


def _raised(frame: object, *, backend: str = "") -> AcpError:
    try:
        _raise_acp_error(frame, backend=backend)
    except AcpError as exc:
        return exc
    raise AssertionError("_raise_acp_error did not raise")


# ── which errors are the model road ───────────────────────────────────────


def test_a_proxy_5xx_that_names_a_credential_fetch_failure_is_the_model_road():
    exc = _raised(PROMPT_FRAME)
    # Premise: it reads as a transient 5xx, in Crew's wording, and stays one.
    assert exc.transient is True and exc.user_worded is True
    assert "transient error (HTTP 5xx)" in str(exc) and "Credential" not in str(exc)
    assert model_road_down_cause(exc) == ce.MODEL_ROAD_CREDENTIALS_UNAVAILABLE


def test_the_frame_a_client_raises_verbatim_is_read_too():
    exc = AcpError(REQUEST_FRAME_TEXT, code=-32603)
    assert model_road_down_cause(exc) == ce.MODEL_ROAD_CREDENTIALS_UNAVAILABLE


def test_credentials_a_process_cannot_reload_are_the_model_road():
    frame = {
        "code": -32603,
        "message": "Internal error",
        "data": {"errorKind": "cloud_credential_error"},
    }
    assert model_road_down_cause(_raised(frame)) == ce.MODEL_ROAD_CREDENTIALS_UNAVAILABLE


@pytest.mark.parametrize(
    "exc",
    [
        AcpAuthRequired("Sign in again."),
        _raised({"code": -32000, "message": "Request failed", "data": "HTTP 401 session expired"}),
        _raised({"code": -32603, "message": "Internal error", "data": "ExpiredTokenException"}),
    ],
    ids=["signed-out harness", "session expiry", "expired token"],
)
def test_a_signed_out_account_is_the_model_road(exc):
    assert model_road_down_cause(exc) == ce.MODEL_ROAD_SIGNED_OUT


def test_credentials_the_service_refused_are_the_model_road():
    frame = {"code": -32603, "message": "Internal error", "data": "AccessDeniedException: no"}
    exc = _raised(frame)
    assert exc.auth_error_code  # premise: the raise read the exception name
    assert model_road_down_cause(exc) == ce.MODEL_ROAD_CREDENTIALS_REFUSED


@pytest.mark.parametrize(
    "exc",
    [
        _raised(PLAIN_5XX_FRAME),
        _raised({"code": -32603, "message": "Internal error", "data": "ThrottlingException"}),
        _raised(
            {
                "code": -32603,
                "message": "Internal error",
                "data": "The security token included in the request is invalid",
            }
        ),
        AcpError("ACP process exited (code=1)"),
        RuntimeError(f"a tool printed {_UNMINTED}"),
        # Curated wording with no frame kept: prose is never read.
        AcpError(f"The helper said {_UNMINTED}", user_worded=True),
    ],
    ids=["plain 5xx", "throttle", "propagation race", "dead process", "not acp", "prose only"],
)
def test_any_other_failure_is_not_the_model_road(exc):
    assert model_road_down_cause(exc) == ""


def test_only_a_providers_own_exception_name_counts():
    assert ce.is_credential_retrieval_failure("botocore: NoCredentialsError: none found")
    assert ce.is_credential_retrieval_failure("CredentialsProviderError: no provider")
    assert not ce.is_credential_retrieval_failure("could not retrieve credentials, try again")
    assert not ce.is_credential_retrieval_failure("MyCredentialRetrievalErrorHandler ran")


# ── the turn: what reaches the ledger ─────────────────────────────────────


async def _moving() -> dict[str, str]:
    ids = await two_by_two()
    status, _ = await _report(WORKER_A, {"status": "progress", "summary": "Suites running."})
    assert status == 200
    assert f"item={ids['item_a']}" not in _conductor_wakes(CONDUCTOR_A)
    return ids


class _Turns:
    """Run whole dashboard turns of one chat against a backend that only fails."""

    def __init__(self, tmp_path: Any, monkeypatch: Any, slot_key: str, failure: Any) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
        self.prompts = 0

        async def _stream(_msg: Any):
            self.prompts += 1
            raise failure()
            yield  # pragma: no cover

        self.state = _TransientSuite._make_state(tmp_path, monkeypatch)
        _TransientSuite._wire_sessions(self.state, _TransientSuite._client(_stream))
        self.slot = self.state.get_or_create_slot(slot_key)
        self.slot._titled = True

    async def run(self, text: str = "carry on") -> None:
        from kiro_crew.dashboard.chat import _run_chat

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(self.state, self.slot, text)
            await _TransientSuite._drain_bg(self.state)

    def error_rows(self) -> list[str]:
        return [m["content"] for m in self.slot.messages if m.get("role") == "error"]


@pytest.mark.asyncio
async def test_a_bound_workers_turn_that_dies_on_unminted_credentials_marks_its_task_blocked(
    tmp_path, monkeypatch
):
    ids = await _moving()
    before_b = wl.item_path(CONDUCTOR_B, ids["item_b"]).read_bytes()
    turns = _Turns(tmp_path, monkeypatch, WORKER_A, lambda: _raised(PROMPT_FRAME))

    await turns.run()

    # The retry ladder ran exactly as it did, and the chat shows the row it showed.
    assert turns.prompts == TRANSIENT_RETRIES + 1
    assert any("transient error (HTTP 5xx)" in row for row in turns.error_rows())
    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.reason) == ("blocked", None)
    assert item.summary.startswith("Gateway:")
    assert "could not mint credentials" in item.summary
    assert item.artifacts == {ARTIFACT: UNAVAILABLE}
    # Nothing of the provider's reaches the ledger: no host, no account, no helper text.
    stored = wl.item_path(CONDUCTOR_A, ids["item_a"]).read_text()
    assert "proxy.example.test" not in stored and "custom-process" not in stored
    # It waits on a service, so it wakes the conductor and holds nothing for a person.
    assert _waits(CONDUCTOR_A, ids["item_a"]) is False
    wakes = _conductor_wakes(CONDUCTOR_A)
    assert f"item={ids['item_a']}" in wakes and "status=blocked" in wakes
    # One report for the turn, however many prompts it retried.
    assert len(_reports(CONDUCTOR_A, ids["item_a"])) == 2  # progress, then the block
    assert wl.item_path(CONDUCTOR_B, ids["item_b"]).read_bytes() == before_b


@pytest.mark.asyncio
async def test_a_frame_the_client_raised_verbatim_marks_it_too(tmp_path, monkeypatch):
    ids = await _moving()
    turns = _Turns(
        tmp_path, monkeypatch, WORKER_A, lambda: AcpError(REQUEST_FRAME_TEXT, code=-32603)
    )

    await turns.run()

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.reason) == ("blocked", None)
    assert item.artifacts == {ARTIFACT: UNAVAILABLE}
    assert "demo" not in wl.item_path(CONDUCTOR_A, ids["item_a"]).read_text()


@pytest.mark.asyncio
async def test_a_signed_out_workers_turn_marks_it_blocked(tmp_path, monkeypatch):
    ids = await _moving()
    turns = _Turns(tmp_path, monkeypatch, WORKER_A, lambda: AcpAuthRequired("Sign in again."))

    await turns.run()

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.reason) == ("blocked", None)
    assert "is signed out" in item.summary
    assert item.artifacts == {ARTIFACT: SIGNED_OUT}


@pytest.mark.asyncio
async def test_a_chat_with_no_bound_task_is_unchanged(tmp_path, monkeypatch):
    ids = await _moving()
    before = {
        board: wl.item_path(board, ids[key]).read_bytes()
        for board, key in ((CONDUCTOR_A, "item_a"), (CONDUCTOR_B, "item_b"))
    }
    turns = _Turns(tmp_path, monkeypatch, "chat-unbound", lambda: _raised(PROMPT_FRAME))

    await turns.run()

    assert turns.prompts == TRANSIENT_RETRIES + 1
    assert any("transient error (HTTP 5xx)" in row for row in turns.error_rows())
    for board, key in ((CONDUCTOR_A, "item_a"), (CONDUCTOR_B, "item_b")):
        assert wl.item_path(board, ids[key]).read_bytes() == before[board]


@pytest.mark.asyncio
async def test_a_turn_that_fails_for_another_reason_is_unchanged(tmp_path, monkeypatch):
    ids = await _moving()
    before = wl.item_path(CONDUCTOR_A, ids["item_a"]).read_bytes()
    turns = _Turns(tmp_path, monkeypatch, WORKER_A, lambda: _raised(PLAIN_5XX_FRAME))

    await turns.run()

    assert turns.prompts == TRANSIENT_RETRIES + 1
    assert any("transient error (HTTP 5xx)" in row for row in turns.error_rows())
    assert wl.item_path(CONDUCTOR_A, ids["item_a"]).read_bytes() == before
    assert f"item={ids['item_a']}" not in _conductor_wakes(CONDUCTOR_A)


# ── one mark per outage, and what it leaves standing ──────────────────────


def _state() -> Any:
    state = MagicMock()
    state._background_tasks = set()
    return state


async def _dies(state: Any, exc: BaseException | None = None) -> None:
    """One turn of the worker ends on the model road; await the write it scheduled."""
    from kiro_crew.dashboard.chat_runner import _note_model_road_down

    turn = SimpleNamespace(state=state, slot=SimpleNamespace(key=WORKER_A))
    _note_model_road_down(turn, exc if exc is not None else _raised(PROMPT_FRAME))
    tasks = list(state._background_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)


@pytest.mark.asyncio
async def test_every_later_turn_that_dies_in_the_same_outage_files_nothing():
    ids = await _moving()
    state = _state()

    for _ in range(4):
        await _dies(state)

    assert len(_reports(CONDUCTOR_A, ids["item_a"])) == 2  # progress, then ONE block
    assert _item(CONDUCTOR_A, ids["item_a"]).status == "blocked"


@pytest.mark.asyncio
async def test_a_turn_that_dies_after_the_conductor_answered_is_marked_again(
    a_second_per_stamp,
):
    ids = await _moving()
    state = _state()
    await _dies(state)
    decide = {"action": "decide", "item_id": ids["item_a"], "decision": "Try again now."}
    assert (await _record(CONDUCTOR_A, decide))[0] == 200

    await _dies(state)
    await _dies(state)

    kinds = [e.get("kind") for e in _events(CONDUCTOR_A, ids["item_a"])]
    assert kinds.count("report") == 3  # progress, the block, the block after the answer
    assert _item(CONDUCTOR_A, ids["item_a"]).status == "blocked"


@pytest.mark.asyncio
async def test_the_next_outage_after_the_worker_reported_again_is_a_new_mark():
    ids = await _moving()
    state = _state()
    await _dies(state)
    # The road came back: the worker's own report replaces the gateway's, and the
    # gateway-only key cannot ride on it even when the worker echoes it.
    back = {
        "status": "progress",
        "summary": "Back on it.",
        "artifacts": {"branch": "fix/x", routes.MODEL_ROAD_ARTIFACT: "mine"},
    }
    assert (await _report(WORKER_A, back))[0] == 200
    assert _item(CONDUCTOR_A, ids["item_a"]).artifacts == {"branch": "fix/x"}

    await _dies(state, AcpAuthRequired("Sign in again."))

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert item.status == "blocked" and "is signed out" in item.summary
    # The worker's own pointers are kept beside the gateway's.
    assert item.artifacts == {
        "branch": "fix/x",
        routes.MODEL_ROAD_ARTIFACT: ce.MODEL_ROAD_SIGNED_OUT,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["question", "blocked", "done"])
async def test_a_report_of_the_workers_own_that_holds_the_conductor_stands(status):
    ids = await two_by_two()
    body: dict[str, Any] = {"status": status, "summary": "My own words."}
    if status == "done":
        body["artifacts"] = {"pr": "1"}
    assert (await _report(WORKER_A, body))[0] == 200

    await _dies(_state())

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.summary) == (status, "My own words.")


@pytest.mark.asyncio
async def test_a_question_the_gateway_filed_for_the_person_is_not_replaced():
    ids = await _moving()
    state = _state()
    routes.note_question_card(state, WORKER_A, [{"question": "Which account?", "options": []}])
    await asyncio.wait_for(asyncio.gather(*list(state._background_tasks)), timeout=30)
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True

    await _dies(state)

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.reason) == ("question", "needs_human")
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True


@pytest.mark.asyncio
async def test_a_worker_that_never_reported_is_marked_on_its_first_dead_turn():
    ids = await two_by_two()

    await _dies(_state())

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.reason) == ("blocked", None)
    assert f"item={ids['item_a']}" in _conductor_wakes(CONDUCTOR_A)


@pytest.mark.asyncio
async def test_an_error_that_is_not_the_model_road_hands_the_ledger_nothing():
    ids = await _moving()
    before = wl.item_path(CONDUCTOR_A, ids["item_a"]).read_bytes()
    state = _state()

    await _dies(state, _raised(PLAIN_5XX_FRAME))
    await _dies(state, RuntimeError("a tool crashed"))

    assert not state._background_tasks
    assert wl.item_path(CONDUCTOR_A, ids["item_a"]).read_bytes() == before


@pytest.mark.asyncio
async def test_a_session_that_is_not_a_bound_worker_is_never_written_for():
    await two_by_two()

    assert await routes.record_model_road_down(_state(), "chat-unbound", cause=UNAVAILABLE) == ""


def test_the_ledger_and_the_classifier_spell_the_vocabulary_alike():
    assert routes.MODEL_ROAD_ARTIFACT == ARTIFACT and ARTIFACT in routes.GATEWAY_ARTIFACT_KEYS
    assert ce.MODEL_ROAD_CREDENTIALS_UNAVAILABLE == UNAVAILABLE
    assert ce.MODEL_ROAD_SIGNED_OUT == SIGNED_OUT
    assert set(routes._ROAD_DOWN_WORDS) == {
        ce.MODEL_ROAD_SIGNED_OUT,
        ce.MODEL_ROAD_CREDENTIALS_REFUSED,
        ce.MODEL_ROAD_CREDENTIALS_UNAVAILABLE,
    }


@pytest.mark.asyncio
async def test_a_cause_outside_the_vocabulary_is_never_written():
    ids = await _moving()

    assert await routes.record_model_road_down(_state(), WORKER_A, cause="anything else") == ""

    assert _item(CONDUCTOR_A, ids["item_a"]).status == "progress"
