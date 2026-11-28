"""A chat that Crew moved onto a model it did not ask for says so, once, in its transcript.

Two stamps wrote a row that was missing or untrue:

* A session that refuses or withholds its pin when it starts runs on the harness's
  own default for its whole life. The transcript said so only where the dashboard
  can judge the pin against the session's advertised list; a harness that resolves
  the model from that list gave no verdict, so the chat ran on another model with
  no row at all.
* A turn whose retries are spent walks ``agent.fallback_model`` and the row said the
  model was throttled, whatever ended the turn. A turn that died on its model
  account read as a throttle.

Pinned here: one row per stamp that names the model asked for, the model serving,
why, and how long it lasts; a retry that starts the session again adds none; a
session that took its pin, a warm session and a plain throttle are unchanged.

The failures are faked frames with made-up names, on the doubles of the
model-fallback suite in ``test_dashboard_chat`` and the frames of
``test_work_ledger_model_road``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from test_dashboard_chat import TestRunChatModelFallback as _FallbackSuite
from test_work_ledger_model_road import PLAIN_5XX_FRAME, PROMPT_FRAME, _raised

from kiro_crew.acp.runtime_models import DEFAULT_MODEL
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.llm_helpers import TRANSIENT_RETRIES
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

#: Made-up names: the model a chat asked for, the one its session serves instead,
#: and the one candidate of the fallback chain.
PINNED = "pinned-model"
SERVED = "served-model"
CANDIDATE = "candidate-model"

#: The wire tokens as a reader of the transcript sees them, spelled out so a test
#: fails on the row, not on a name.
PIN_REFUSED = "model_pin_refused"
ACCOUNT_FALLBACK = "model_account_fallback"
THROTTLED_FALLBACK = "model_throttled_fallback"


class _Chat:
    """Whole dashboard turns of one chat on the fallback suite's doubles."""

    def __init__(
        self, tmp_path: Any, monkeypatch: Any, stream: Any, *, chain: tuple[str, ...] = ()
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: chain)
        self.state = _FallbackSuite._make_state(tmp_path, monkeypatch)
        self.client = _FallbackSuite._client(stream, advertised=chain)
        _FallbackSuite._wire_sessions(self.state, self.client)
        self.slot = self.state.get_or_create_slot("s1")
        self.slot._titled = True

    def session_is(self, *, new: bool = False, resumed: bool = False) -> None:
        """What the next turn's session claim answers: a new, a reloaded or a warm one."""
        self.state.sessions.get_or_create = AsyncMock(return_value=(self.client, new, resumed))

    def refuses(self, pin: str, *, serves: str) -> None:
        """The session refused *pin* when it started and runs on *serves*."""
        self.client.capabilities = capabilities_for(ACP_BACKEND_CLAUDE)
        self.client.model_pin_refused = pin
        self.client.served_model = serves
        self.slot.model = pin

    async def run(self, text: str = "hello") -> None:
        from kiro_crew.dashboard.chat import _run_chat

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(self.state, self.slot, text)
            await _FallbackSuite._drain_bg(self.state)

    def notices(self) -> list[dict[str, Any]]:
        return [m for m in self.slot.messages if m.get("role") == "notice"]

    @staticmethod
    def told(row: dict[str, Any]) -> dict[str, Any]:
        """What a row tells the frontend: its meta without the row's own id."""
        return {k: v for k, v in row["meta"].items() if k != "mid"}

    def rows(self) -> list[tuple[str, str]]:
        return [(m.get("role", ""), m.get("content", "")) for m in self.slot.messages]


async def _answers(_msg: Any):
    yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
    yield LLMEvent(kind=EVENT_COMPLETE)


def _fails_then_answers(failure: Any, times: int) -> Any:
    calls = 0

    async def _stream(_msg: Any):
        nonlocal calls
        calls += 1
        if calls <= times:
            raise failure()
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
        yield LLMEvent(kind=EVENT_COMPLETE)

    return _stream


# ── a pin the session refused when it started ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("serves", "named"),
    [(SERVED, SERVED), ("", DEFAULT_MODEL)],
    ids=["the session names what it serves", "the session names nothing"],
)
async def test_a_pin_the_session_refused_at_its_start_is_said_in_the_chat(
    tmp_path, monkeypatch, serves, named
):
    chat = _Chat(tmp_path, monkeypatch, _answers)
    chat.refuses(PINNED, serves=serves)

    await chat.run()

    # Premise: this harness gives no verdict on the pin, so the older row is not written.
    assert chat.slot.model_withheld is None
    notices = chat.notices()
    assert len(notices) == 1, notices
    row = notices[0]
    assert chat.told(row) == {
        "notice": PIN_REFUSED,
        "notice_args": {"model": PINNED, "served": named},
    }
    # Which model it asked for, which serves it, why, and how long that lasts.
    assert row["content"] == (
        f"⚠️ {PINNED} was not accepted when this session started. This chat is running on "
        f"{named} until the session restarts or you pick a model."
    )
    # The pick is kept, so the next session start offers it again.
    assert chat.slot.model == PINNED
    assert ("assistant", "done") in chat.rows()


@pytest.mark.asyncio
async def test_a_retry_that_starts_the_session_again_does_not_say_it_twice(tmp_path, monkeypatch):
    # The claim answers "new session" on every pass, as a retry that respawned would.
    chat = _Chat(
        tmp_path, monkeypatch, _fails_then_answers(lambda: _raised(PLAIN_5XX_FRAME), times=2)
    )
    chat.refuses(PINNED, serves=SERVED)

    await chat.run()

    assert ("assistant", "done") in chat.rows()
    assert [m["meta"]["notice"] for m in chat.notices()] == [PIN_REFUSED]


@pytest.mark.asyncio
async def test_a_session_that_starts_again_on_a_later_turn_says_it_again(tmp_path, monkeypatch):
    chat = _Chat(tmp_path, monkeypatch, _answers)
    chat.refuses(PINNED, serves=SERVED)

    await chat.run("first")
    chat.session_is(resumed=True)
    await chat.run("second")

    assert [m["meta"]["notice"] for m in chat.notices()] == [PIN_REFUSED, PIN_REFUSED]


@pytest.mark.asyncio
async def test_a_warm_session_is_not_told_again(tmp_path, monkeypatch):
    chat = _Chat(tmp_path, monkeypatch, _answers)
    chat.refuses(PINNED, serves=SERVED)

    await chat.run("first")
    chat.session_is()
    await chat.run("second")

    assert len(chat.notices()) == 1


@pytest.mark.asyncio
async def test_a_session_that_took_its_pin_is_unchanged(tmp_path, monkeypatch):
    chat = _Chat(tmp_path, monkeypatch, _answers)
    chat.refuses("", serves=PINNED)
    chat.slot.model = PINNED

    await chat.run()

    assert ("assistant", "done") in chat.rows()
    # Nothing but the reply: no notice row and no error row.
    assert [row for row in chat.rows() if row[0] in ("notice", "error")] == []


# ── a turn that walked the fallback chain ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_that_died_on_its_model_account_names_the_account_not_a_throttle(
    tmp_path, monkeypatch
):
    chat = _Chat(
        tmp_path,
        monkeypatch,
        _fails_then_answers(lambda: _raised(PROMPT_FRAME), times=TRANSIENT_RETRIES + 1),
        chain=(CANDIDATE,),
    )
    was = chat.client._model

    await chat.run()

    # The walk itself is as it was: one swap, and the turn lands on the candidate.
    chat.client.set_model.assert_awaited_once_with(CANDIDATE)
    assert chat.slot._active_fallback_model == CANDIDATE
    assert ("assistant", "done") in chat.rows()
    notices = chat.notices()
    assert len(notices) == 1, notices
    row = notices[0]
    assert chat.told(row) == {
        "notice": ACCOUNT_FALLBACK,
        "notice_args": {"primary": was, "fallback": CANDIDATE},
    }
    assert row["content"] == (
        f"⚠️ {was} could not be used: its model account could not sign in. This chat is "
        f"running on {CANDIDATE} until the next turn starts, when {was} is tried again."
    )
    # Nothing of the provider's reaches the transcript: no host, no helper text.
    assert "proxy.example.test" not in row["content"] and "custom-process" not in row["content"]


@pytest.mark.asyncio
async def test_a_throttled_turn_keeps_its_row_byte_for_byte(tmp_path, monkeypatch):
    chat = _Chat(
        tmp_path,
        monkeypatch,
        _fails_then_answers(lambda: _raised(PLAIN_5XX_FRAME), times=TRANSIENT_RETRIES + 1),
        chain=(CANDIDATE,),
    )
    was = chat.client._model

    await chat.run()

    notices = chat.notices()
    assert len(notices) == 1, notices
    assert notices[0]["content"] == (
        f"⚠️ {was} is throttled — running on {CANDIDATE} until {was} recovers."
    )
    assert chat.told(notices[0]) == {
        "notice": THROTTLED_FALLBACK,
        "notice_args": {"primary": was, "fallback": CANDIDATE},
    }
