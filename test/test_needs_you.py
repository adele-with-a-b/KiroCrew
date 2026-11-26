"""The sidebar row's ``needs_you`` summary: what waits on the owner, and when it clears."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import needs_you, slot_projection
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history_schema import HUMAN_TURN_META_KEY
from kiro_crew.testing.clock import ManualClock

#: The frozen "now" every test reads: 2027-01-15T08:00:00Z.
NOW = 1_800_000_000.0


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    ManualClock(NOW).install(monkeypatch, slot_projection)


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    created = asyncio.new_event_loop()
    yield created
    created.close()


def _person(text: str, at: float) -> dict:
    return {"role": "user", "content": text, "ts": _stamp(at), "meta": {HUMAN_TURN_META_KEY: True}}


def _reply(text: str, at: float, mid: str = "m-reply") -> dict:
    return {"role": "assistant", "content": text, "ts": _stamp(at), "meta": {"mid": mid}}


def _slot(*rows: dict) -> _ChatSlot:
    slot = _ChatSlot("chat-1")
    slot.messages.extend(rows)
    return slot


# ── the label parser ──


def test_labels_open_a_line_after_list_heading_or_bold_marks():
    text = "\n".join(
        [
            "Two things.",
            "[Q1a] Ship tonight? I would.",
            "- **[Q1b] Keep the flag?** Yes, for a week.",
            "### [Q2a] Rename it",
            "1. [Q3c] Which date format?",
            "  * _[Q4a]_ Indented under a list.",
            "[Q1a] The same label again counts once.",
        ]
    )
    assert needs_you.question_labels(text) == [
        ("Q1a", "Q1a Ship tonight? I would."),
        ("Q1b", "**Q1b Keep the flag?** Yes, for a week."),
        ("Q2a", "Q2a Rename it"),
        ("Q3c", "Q3c Which date format?"),
        ("Q4a", "_Q4a _ Indented under a list."),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Should I ship it tonight?",
        "[Q1] No letter.",
        "[Q1ab] Two letters.",
        "[q1a] Lower-case Q.",
        "[R1a] Not a question label.",
        "See [Q1a] in the middle of a line.",
        "> [Q1a] Quoted.",
        "    [Q1a] An indented code line.",
        "\t[Q1a] A tab-indented code line.",
        "`[Q1a]` In inline code.",
        "[Q1a](https://example.invalid) A link named like a label.",
        "```\n[Q1a] In a fence.\n```",
        "~~~text\n[Q1a] In a tilde fence.\n~~~",
        "````\n```\n[Q1a] Still in the outer fence.\n````",
    ],
)
def test_text_that_is_not_a_question_label(text: str):
    assert needs_you.question_labels(text) == []


def test_a_label_after_a_closed_fence_counts():
    text = "```\n[Q1a] code\n```\n[Q2a] After the fence?"
    assert [label for label, _ in needs_you.question_labels(text)] == ["Q2a"]


def test_a_fence_the_walker_cannot_read_ends_the_count():
    # A container-prefixed fence may open code the walker cannot see, so
    # nothing after it counts: an unsure line is not an ask.
    text = "[Q1a] Before it?\n- ```\n[Q2a] After it?"
    assert [label for label, _ in needs_you.question_labels(text)] == ["Q1a"]


def test_options_words_start_at_the_last_question_of_the_last_paragraph():
    body = (
        "Screenshots are retaken.\n\n"
        "The old page has no links left. Drop the old page, or keep a redirect? "
        "I would drop it."
    )

    def keep(text: str) -> str:
        return text

    assert needs_you.options_words(body, keep) == (
        "Drop the old page, or keep a redirect? I would drop it."
    )
    assert needs_you.options_words("All done.\n\nPick one below.", keep) == "Pick one below."
    assert needs_you.options_words("", keep) == ""


def test_options_words_mask_each_line_as_the_chat_masks_it():
    # The redactor masks a ``key=value`` credential only at the start of a line, so
    # a line under the question must be masked before the paragraph is joined.
    from kiro_crew.dashboard.state import _redact
    from kiro_crew.testing.synthetic_credentials import synth

    secret = synth(24, seed=5)
    line = f"api_key={secret}"
    assert secret not in _redact(f"Use this one?\n{line}")
    slot = _slot(
        _person("Which key goes in the config?", NOW - 120),
        _reply(f"Found one.\n\nUse this one?\n{line}\n[OPTIONS: Use it | Find another]", NOW - 60),
    )
    words = slot.to_dict()["needs_you"]["first_words"]
    assert secret not in words
    assert words.startswith("Use this one? ")


def _key_block() -> tuple[str, list[str]]:
    """A private-key block spread over lines, and its body lines (no literal key here)."""
    from kiro_crew.testing.synthetic_credentials import B64, synth

    kind = "PRIVATE" + " KEY"
    body = [synth(64, B64, seed=n) for n in range(3)]
    return "\n".join([f"-----BEGIN RSA {kind}-----", *body, f"-----END RSA {kind}-----"]), body


def _shows_any(words: str, body: list[str]) -> bool:
    # The words are cut short, so a leak shows only the start of a body line.
    return any(line[:12] in words for line in body)


def test_options_words_keep_a_key_block_masked():
    # Masking each line on its own misses a key block spread over several lines.
    from kiro_crew.dashboard.state import _redact

    block, body = _key_block()
    assert not _shows_any(_redact(f"Use this key?\n{block}"), body)  # the chat masks it
    slot = _slot(
        _person("Which key goes in the config?", NOW - 120),
        _reply(f"Found one.\n\nUse this key?\n{block}\n[OPTIONS: Use it | Find another]", NOW - 60),
    )
    words = slot.to_dict()["needs_you"]["first_words"]
    assert words.startswith("Use this key? ")
    assert not _shows_any(words, body)


def test_clean_words_strip_then_redact_then_cut():
    seen: list[str] = []

    def redact(text: str) -> str:
        seen.append(text)
        return text.replace("secret", "[redacted]")

    assert needs_you.clean_words("**Use** the `secret`?", redact) == "Use the [redacted]?"
    # preview_text.masked_preview: once with the lines kept, once on the joined line.
    assert seen == ["Use the secret?", "Use the [redacted]?"]
    long = needs_you.clean_words("abcdefghij" * 10, lambda text: text)
    assert long == "abcdefghij" * 7 + "abcdefghi…"
    assert len(long) == needs_you.FIRST_WORDS_MAX


# ── kinds 3 and 4: the newest finished reply ──


def test_options_reply_to_a_person_waits_on_the_owner():
    slot = _slot(
        _person("Refresh the docs.", NOW - 120),
        _reply(
            "Done.\n\nDrop the old page, or keep a redirect? I would drop it.\n"
            "[OPTIONS: Drop it | Keep a redirect]",
            NOW - 60,
        ),
    )
    assert slot.to_dict()["needs_you"] == {
        "count": 1,
        "oldest_ts": NOW - 60,
        "first_words": "Drop the old page, or keep a redirect? I would drop it.",
        "row_id": "m-reply",
        "kinds": ["options"],
    }


def test_each_distinct_label_is_one_ask():
    slot = _slot(
        _person("Plan the release.", NOW - 300),
        _reply(
            "Ready.\n[Q4a] Ship 0.13.2 tonight? I would.\n[Q4b] Tag it too?\n[Q4a] Again.",
            NOW - 240,
        ),
    )
    assert slot.to_dict()["needs_you"] == {
        "count": 2,
        "oldest_ts": NOW - 240,
        "first_words": "Q4a Ship 0.13.2 tonight? I would.",
        "row_id": "m-reply",
        "kinds": ["labelled"],
    }


def test_labels_with_an_options_line_count_the_labels():
    slot = _slot(
        _person("Go on.", NOW - 30),
        _reply("[Q1a] Merge now?\n[OPTIONS: Merge it | Wait]", NOW - 20),
    )
    value = slot.to_dict()["needs_you"]
    assert value["count"] == 1
    assert value["kinds"] == ["options", "labelled"]
    assert value["first_words"] == "Q1a Merge now?"


def test_a_plain_question_counts_nothing():
    slot = _slot(_person("Hi.", NOW - 30), _reply("Shall I go on?", NOW - 20))
    assert slot.to_dict()["needs_you"] is None


def test_the_owners_next_message_clears_it():
    slot = _slot(
        _person("Go.", NOW - 30),
        _reply("[Q1a] Merge now?", NOW - 20),
    )
    assert slot.to_dict()["needs_you"] is not None
    slot.messages.append(_person("Yes.", NOW - 10))
    assert slot.to_dict()["needs_you"] is None


def test_a_reply_to_another_sessions_send_does_not_ask_the_owner():
    relay = {"role": "user", "content": "[From chat-2] Do the review.", "ts": _stamp(NOW - 30)}
    slot = _slot(relay, _reply("[Q1a] Approve the plan?\n[OPTIONS: Yes | No]", NOW - 20))
    assert slot.to_dict()["needs_you"] is None


def test_gateway_input_rows_carry_the_persons_turn_on():
    slot = _slot(
        _person("Run the checks.", NOW - 90),
        _reply("Started two checks.", NOW - 80, mid="m-first"),
        {"role": "subagent", "content": "[Subagent completion event] done", "ts": _stamp(NOW - 70)},
        _reply("Both passed.\n[OPTIONS: Merge | Hold]", NOW - 60),
    )
    assert slot.to_dict()["needs_you"]["kinds"] == ["options"]


def test_a_long_row_before_the_reply_keeps_the_summary_memoized():
    """Who started the turn is read outside the memoized summary, so a big row
    between the person's message and the reply does not stop the memo."""
    from kiro_crew.dashboard import slot_row_memo

    slot = _slot(
        _person("Fetch the log.", NOW - 90),
        {
            "role": "tool_result",
            "content": "x" * (slot_row_memo.MAX_MEMO_TEXT + 1),
            "ts": _stamp(NOW - 80),
        },
        _reply("[Q1a] Keep the log?", NOW - 60),
    )
    context = slot_row_memo.RowMemoContext(redaction=("stamp",), links=None)
    token = slot_row_memo.ROW_MEMO.set(context)
    try:
        assert slot.to_dict()["needs_you"]["kinds"] == ["labelled"]
        assert slot._row_summary_memo is not None
        assert slot.to_dict()["needs_you"]["kinds"] == ["labelled"]
    finally:
        slot_row_memo.ROW_MEMO.reset(token)
    assert context.counts["summary_hit"] == 1


def test_no_person_in_reach_reads_as_not_the_owners():
    rows = [{"role": "nudge", "content": "cycle", "ts": _stamp(NOW - 500)}]
    rows += [
        {"role": "tool_call", "content": "", "ts": _stamp(NOW - 400)}
        for _ in range(needs_you.STARTER_SCAN_ROWS)
    ]
    slot = _slot(_person("Watch it.", NOW - 600), *rows, _reply("[Q1a] Stop?", NOW - 10))
    assert slot.to_dict()["needs_you"] is None


def test_a_running_turn_and_an_old_reply_do_not_wait():
    rows = [_person("Go.", NOW - 9), _reply("[Q1a] Go?", NOW - 5)]
    reply = needs_you.ReplyAsk(_stamp(NOW - 5), ("Q1a",), False, "Q1a Go?", "m", 0)

    def no_notice(role: str, meta: dict) -> bool:
        return False

    assert needs_you.reply_asks(rows, reply, open_now=True, is_system_notice=no_notice)
    assert needs_you.reply_asks(rows, reply, open_now=False, is_system_notice=no_notice) == []
    slot = _slot(
        _person("Go.", NOW - 9 * 24 * 3600),
        _reply("[Q1a] Go?\n[OPTIONS: Go | Stop]", NOW - 8 * 24 * 3600),
    )
    assert slot.to_dict()["needs_you"] is None


def test_running_turn_hides_the_reply_ask(loop: asyncio.AbstractEventLoop):
    slot = _slot(_person("Go.", NOW - 30), _reply("[Q1a] Go?", NOW - 20))
    slot.task = loop.create_future()
    assert slot.to_dict()["needs_you"] is None


# ── kinds 1 and 2: approvals and question cards ──


def _permission(slot: _ChatSlot, request_id: str, at: float, purpose: str = "") -> dict:
    meta = {"request_id": request_id, "tool_input": "ls"}
    if purpose:
        meta["tool_purpose"] = purpose
    row = {
        "role": "permission",
        "content": "Bash",
        "cls": json.dumps(meta),
        "ts": _stamp(at),
        "meta": {"mid": f"m-{request_id}"},
    }
    slot.messages.append(row)
    return row


def test_pending_approvals_count_and_name_the_oldest(loop: asyncio.AbstractEventLoop):
    slot = _slot(_person("Run the shard.", NOW - 200))
    first = _permission(slot, "r1", NOW - 120, purpose="run shard 4 in a test home")
    second = _permission(slot, "r2", NOW - 60)
    slot.register_approval("r1", loop.create_future(), first)
    slot.register_approval("r2", loop.create_future(), second)
    assert slot.to_dict()["needs_you"] == {
        "count": 2,
        "oldest_ts": NOW - 120,
        "first_words": "Bash · run shard 4 in a test home",
        "row_id": "m-r1",
        "kinds": ["approval"],
    }


def test_a_decided_approval_clears(loop: asyncio.AbstractEventLoop):
    slot = _slot(_person("Run it.", NOW - 200))
    future = loop.create_future()
    slot.register_approval("r1", future, _permission(slot, "r1", NOW - 120))
    assert slot.to_dict()["needs_you"]["count"] == 1
    future.set_result("approve")
    assert slot.to_dict()["needs_you"] is None


def test_coordinator_approval_and_question_card(loop: asyncio.AbstractEventLoop):
    slot = _slot(_person("Go.", NOW - 900))
    slot._coordinator_approvals = lambda key: [
        {"id": "spawn:1", "tool": "spawn", "tool_purpose": "review", "ts": NOW - 300}
    ]
    slot._question_pending = {
        "card-1": {
            "ts": NOW - 400,
            "blocking": True,
            "questions": [{"question": "Which date format should the export use?"}],
        }
    }
    assert slot.to_dict()["needs_you"] == {
        "count": 2,
        "oldest_ts": NOW - 400,
        "first_words": "Which date format should the export use?",
        "row_id": None,
        "kinds": ["approval", "question"],
    }
    slot._question_pending = {}
    slot._coordinator_approvals = lambda key: []
    assert slot.to_dict()["needs_you"] is None


def test_an_idle_chat_waits_on_nobody():
    slot = _slot(_person("Thanks.", NOW - 20), _reply("You are welcome.", NOW - 10))
    assert slot.to_dict()["needs_you"] is None
    assert _ChatSlot("chat-2").to_dict()["needs_you"] is None


# ── the Crew Mode half, on the whole list ──


def _worker(loop: asyncio.AbstractEventLoop) -> _ChatSlot:
    slot = _ChatSlot("worker-1")
    relay = {"role": "user", "content": "[From root] Build it.", "ts": _stamp(NOW - 300)}
    slot.messages.append(relay)
    slot.register_approval("r1", loop.create_future(), _permission(slot, "r1", NOW - 200))
    return slot


def _rows(*slots: _ChatSlot) -> list[dict]:
    return [slot.to_dict() for slot in slots]


def test_a_workers_approval_moves_onto_its_listed_conductor(loop: asyncio.AbstractEventLoop):
    root = _slot(_person("Lead the crew.", NOW - 50), _reply("[Q9a] Start?", NOW - 40))
    root.key = "root-1"
    worker = _worker(loop)
    rows = _rows(root, worker)
    rows[0]["crew_mode_root"] = True
    rows[1]["crew_root"] = "root-1"
    state = SimpleNamespace(_slots={"root-1": root, "worker-1": worker})
    needs_you.settle_crew_rows(state, rows)
    assert rows[1]["needs_you"] is None
    # The permission row is in the worker's transcript, so the conductor's row
    # names none.
    assert rows[0]["needs_you"] == {
        "count": 2,
        "oldest_ts": NOW - 200,
        "first_words": "Bash",
        "row_id": None,
        "kinds": ["approval", "labelled"],
    }


def test_a_worker_keeps_its_approval_while_its_conductor_is_not_listed(
    loop: asyncio.AbstractEventLoop,
):
    worker = _worker(loop)
    worker.messages.append(_person("Unblock it.", NOW - 150))
    worker.messages.append(_reply("[Q1a] Use the cache?", NOW - 100))
    rows = _rows(worker)
    rows[0]["crew_root"] = "root-closed"
    needs_you.settle_crew_rows(SimpleNamespace(_slots={"worker-1": worker}), rows)
    # Its reply asks are its conductor's to answer; the approval stays here.
    assert rows[0]["needs_you"]["kinds"] == ["approval"]
    assert rows[0]["needs_you"]["count"] == 1


def test_a_settle_fault_keeps_every_rows_own_value():
    rows = [{"key": "worker-1", "crew_root": "root-1", "needs_you": {"count": 1}}]
    needs_you.settle_crew_rows(SimpleNamespace(_slots={"worker-1": object()}), rows)
    assert rows[0]["needs_you"] == {"count": 1}


@pytest.mark.asyncio
async def test_the_slots_list_settles_workers_after_the_crew_stamps(tmp_path, monkeypatch):
    """``serialize_slots`` settles the worker rows once ``crew_mode.stamp_rows`` ran.

    Async because ``get_or_create_slot`` may push a slots update, which needs a
    running loop.
    """
    from chat_test_helpers import _make_state

    from kiro_crew import crew_mode

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.get_or_create_slot("root-1")
    worker = state.get_or_create_slot("worker-1")
    worker.messages.append(
        {"role": "user", "content": "[From root] Build it.", "ts": _stamp(NOW - 300)}
    )
    future = asyncio.get_running_loop().create_future()
    worker.register_approval("r1", future, _permission(worker, "r1", NOW - 200))

    def stamp(rows, **_kwargs):
        for row in rows:
            row[crew_mode.ROOT_FIELD] = row["key"] == "root-1"
            row[crew_mode.WORKER_FIELD] = "root-1" if row["key"] == "worker-1" else ""

    monkeypatch.setattr(crew_mode, "stamp_rows", stamp)
    rows = {row["key"]: row for row in state.serialize_slots()}
    future.cancel()
    assert rows["worker-1"]["needs_you"] is None
    assert rows["root-1"]["needs_you"]["kinds"] == ["approval"]
    assert rows["root-1"]["needs_you"]["oldest_ts"] == NOW - 200


def test_approval_words_mask_what_the_approval_card_masks(loop: asyncio.AbstractEventLoop):
    # The redactor masks a ``key=value`` credential only at the start of a line, so
    # redacting "tool · purpose" after the join would let one through.
    from kiro_crew.testing.synthetic_credentials import synth

    secret = synth(24, seed=5)
    purpose = f"api_key={secret}"

    direct = _slot(_person("Deploy it.", NOW - 300))
    direct.register_approval(
        "r1", loop.create_future(), _permission(direct, "r1", NOW - 200, purpose=purpose)
    )
    coordinator = _slot(_person("Deploy it.", NOW - 300))
    coordinator._coordinator_approvals = lambda key: [
        {"id": "spawn:1", "tool": "Bash", "tool_purpose": purpose, "ts": NOW - 200}
    ]
    for slot in (direct, coordinator):
        row = slot.to_dict()
        masked = row["pending_approval_info"]["tool_purpose"]
        assert secret not in masked
        assert row["needs_you"]["first_words"] == f"Bash · {masked}"

    root = _slot(_person("Lead the crew.", NOW - 400))
    root.key = "root-1"
    worker = _ChatSlot("worker-1")
    worker.messages.append(
        {"role": "user", "content": "[From root] Deploy.", "ts": _stamp(NOW - 300)}
    )
    worker.register_approval(
        "r1", loop.create_future(), _permission(worker, "r1", NOW - 200, purpose=purpose)
    )
    rows = _rows(root, worker)
    rows[0]["crew_mode_root"] = True
    rows[1]["crew_root"] = "root-1"
    needs_you.settle_crew_rows(SimpleNamespace(_slots={"root-1": root, "worker-1": worker}), rows)
    masked = rows[1]["pending_approval_info"]["tool_purpose"]
    assert secret not in masked
    assert rows[0]["needs_you"]["first_words"] == f"Bash · {masked}"


def test_approval_words_keep_a_key_block_masked(loop: asyncio.AbstractEventLoop):
    block, body = _key_block()
    purpose = f"write this key:\n{block}"
    direct = _slot(_person("Deploy it.", NOW - 300))
    direct.register_approval(
        "r1", loop.create_future(), _permission(direct, "r1", NOW - 200, purpose=purpose)
    )
    coordinator = _slot(_person("Deploy it.", NOW - 300))
    coordinator._coordinator_approvals = lambda key: [
        {"id": "spawn:1", "tool": "Bash", "tool_purpose": purpose, "ts": NOW - 200}
    ]
    for slot in (direct, coordinator):
        words = slot.to_dict()["needs_you"]["first_words"]
        assert words.startswith("Bash · write this key: ")
        assert not _shows_any(words, body)
