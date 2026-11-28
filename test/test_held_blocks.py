"""A Conductor's context is sent each fixed block once, not at every wake.

Measured on a live Conductor conversation over one day: 37% of the characters Crew
sent it were lines the same context already held. Six causes, each pinned here on
the route that delivers it:

1. a Stop on Claude Code re-armed the whole after-compaction re-send, though the
   conversation kept the stopped prompt, so every message waiting behind the Stop
   carried the re-send again (``TestAStoppedPromptTheHarnessKept``);
2. every wake, fired or steered, carried its how-to lines, the patrol instruction
   and the ``[seats]`` instruction (``TestALaterWakeIsItsNews``,
   ``TestWakesThatWaitedTogether``);
3. every wake carried the ``[seats]`` fact line, moved or not (``TestTheSeatsLine``);
4. every resumed turn carried the resumed header, the critical rules, the response
   preferences and the reply-format rules (``TestAResumedTurn``).

5. every fired wake opened with the whole session-ledger snapshot, though no row of
   it had moved (``TestTheLedgerSnapshot``, ``TestWhereTheSnapshotRides``);
6. nearly every turn carried the project line and the runtime line
   (``TestTheProjectAndRuntimeLines``).

"Once per context" is the model's context: ``TestWhatEndsAContext`` covers a
compaction, a context that starts empty and a wake shortened against a context that
turned out to be gone. ``TestAChatWithNoRecord`` holds every other chat to the text
it had before, byte for byte.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import conductor_patrol
from kiro_crew import context as ctx
from kiro_crew import ledger_wake
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import EVENT_COMPLETE, JsonRpcMessage
from kiro_crew.autonudge import NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_runner, patrol_seats
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.monitoring.models import MONITOR_STATE_VERSION, MonitorState
from kiro_crew.providers.acp_turns import _to_llm_event
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack import gateway as gw

try:
    from kiro_crew import held_blocks
except ImportError:  # a tree without the record sends every block every time
    held_blocks = None  # type: ignore[assignment]

CONDUCTOR = "chat-conductor"
KEY = "dashboard:chat-conductor"
SUFFIX = (
    "The work-ledger watch stays armed. Read the items with work_ledger_read "
    "before acting: this text names what changed, not the whole board."
)
POINTER = ledger_wake.WAKE_READ_POINTER
PATROL = conductor_patrol.PATROL_MESSAGE
SEATS_HOW_TO = patrol_seats.SEATS_INSTRUCTION
RESUMED = "[SESSION RESUMED"
REPLY_RULES = "[REPLY FORMAT RULES]"

pytestmark = pytest.mark.usefixtures("ample_host_resources")


@pytest.fixture(autouse=True)
def _own_home_and_no_records(tmp_path, _floor_monkeypatch, close_skills_loaders):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield
    if held_blocks is not None:
        # Not ``drop``: that keeps a shortened wake no turn kept, for its retry.
        held_blocks._records.pop(KEY, None)


def _wake(item: str) -> str:
    """A work-ledger wake as the claim and the composer deliver it."""
    return ledger_wake.wake_turn_text(f"[work-ledger wake] item={item} status=done\n\n{SUFFIX}")


def _seats(ready: str) -> str:
    return patrol_seats.seats_block(ready=[ready], local_room="ample")


def _turn_kept(*prompts: str, fresh: bool = False) -> None:
    """What a Conductor turn's build and its kept end leave in the record."""
    if held_blocks is None:
        return
    held_blocks.begin_turn(KEY, fresh=fresh)
    for prompt in prompts:
        held_blocks.note_wake(KEY, prompt)
    held_blocks.settle_turn(KEY, kept=True)


# ── the fire, as test_work_ledger_wake_delivery drives it ──────────────────


def _orchestrator() -> gw.GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = SimpleNamespace(
        get_slot=MagicMock(return_value=None),
        push_slots_update=MagicMock(),
        _background_tasks=set(),
        run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
        _slots={},
        _restricted_keys=set(),
        sessions=None,
    )
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    orch.autonudge_svc.monitor_dispatch_is_authorized = AsyncMock(return_value=True)
    orch._session_tasks = {}
    return orch


def _slot(*, running: bool = False) -> MagicMock:
    slot = MagicMock()
    slot.key = CONDUCTOR
    slot.linked_session_key = ""
    slot.running = running
    slot.is_closing = False
    slot.mode = ""
    slot.memory_mode = "persistent"
    slot._last_turn_structural_terminal = False
    slot._last_turn_structural_terminal_loop_id = ""
    slot._last_turn_structural_terminal_loop_gen = 0
    return slot


def _loop() -> NudgeLoop:
    return NudgeLoop(
        id="loop-wl",
        slot_key=CONDUCTOR,
        message=PATROL,
        idle_secs=1200,
        monitor=MonitorState(
            kind="work-ledger",
            target=CONDUCTOR,
            objective="review_ready",
            created_ts=1_000.0,
            version=MONITOR_STATE_VERSION,
        ),
        gate=True,
    )


def _spawn_nothing(state, slot, coro, **kwargs):
    coro.close()
    return MagicMock(name="turn-task")


async def _fired(wake: str, seats: str = "", *, slot: MagicMock | None = None) -> str:
    """The prompt one fire starts an idle Conductor's turn with."""
    orch = _orchestrator()
    slot = slot or _slot()
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)
    with (
        patch.object(gw, "spawn_guarded_turn", _spawn_nothing),
        patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value=wake)),
        patch.object(patrol_seats, "seats_text", AsyncMock(return_value=seats)),
    ):
        assert await orch._fire_dashboard_nudge(_loop()) is True
    rows = [call for call in slot.append.call_args_list if call.args[0] == "nudge"]
    assert rows, "the nudge row was never written"
    return str(rows[0].args[1])


async def _steered(wake: str, seats: str = "", *, outcome: str = "steered") -> str:
    """The text one fire steers into a Conductor's running turn."""
    orch = _orchestrator()
    slot = _slot(running=True)
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)
    steer = AsyncMock(return_value=outcome)
    with (
        patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer),
        patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
        patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value=wake)),
        patch.object(patrol_seats, "seats_text", AsyncMock(return_value=seats)),
    ):
        assert await orch._fire_dashboard_nudge(_loop()) is True
    return str(steer.await_args.args[2])


def test_the_key_the_fire_reads_is_the_one_the_turn_writes():
    assert effective_session_key(_slot()) == KEY


class TestALaterWakeIsItsNews:
    @pytest.mark.asyncio
    async def test_a_fired_wake_after_the_first_is_its_news_and_its_header(self):
        """Point 2: the how-to lines and the patrol instruction, once per context."""
        _turn_kept(fresh=True)
        first = await _fired(_wake("it_1"))
        for block in (SUFFIX, POINTER, PATROL):
            assert first.count(block) == 1, "the first wake of a context carries every block"
        _turn_kept(first)

        later = await _fired(_wake("it_2"))

        assert "item=it_2 status=done" in later and "[auto-nudge cycle" in later
        for block in (SUFFIX, POINTER, PATROL):
            assert block not in later, "a line this context already holds went out again"
        assert later == "[work-ledger wake] item=it_2 status=done\n\n[auto-nudge cycle 1]"

    @pytest.mark.asyncio
    async def test_a_steered_wake_leaves_out_what_the_context_holds(self):
        _turn_kept(await _fired(_wake("it_1")), fresh=True)

        steered = await _steered(_wake("it_2"))

        assert steered == "[work-ledger wake] item=it_2 status=done"

    @pytest.mark.asyncio
    async def test_two_steers_into_one_turn_carry_the_lines_once(self):
        """The first wake of a context is steered: the second must not repeat it."""
        _turn_kept(fresh=True)
        first = await _steered(_wake("it_1"))
        assert SUFFIX in first and POINTER in first

        second = await _steered(_wake("it_2"))

        assert second == "[work-ledger wake] item=it_2 status=done"

    @pytest.mark.asyncio
    async def test_a_steer_the_turn_never_read_does_not_count(self):
        """It became a queue card, so the context does not hold its lines."""
        _turn_kept(fresh=True)
        await _steered(_wake("it_1"))
        held_blocks.settle_turn(KEY, kept=True)  # the turn ended; nothing was read

        later = await _fired(_wake("it_2"))

        assert SUFFIX in later and POINTER in later

    @pytest.mark.asyncio
    async def test_a_steer_the_turn_read_counts(self, tmp_path, monkeypatch):
        """Through the echo that settles a steer: the one point that knows it was read."""
        from chat_test_helpers import _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        slot = _make_state(tmp_path).get_or_create_slot(CONDUCTOR)
        assert effective_session_key(slot) == KEY
        _turn_kept(fresh=True)
        first = await _steered(_wake("it_1"))
        slot._pending_steers = [first]
        slot._steer_automation = {first: {"cycle": 1, "loop_id": "loop-wl"}}
        with patch.object(chat_runner, "settle_consumed_steers", return_value=[]):
            chat_runner._settle_consumed_steers(slot, first)
        held_blocks.settle_turn(KEY, kept=False)  # read is read, however the turn ends

        later = await _fired(_wake("it_2"))

        assert SUFFIX not in later and POINTER not in later

    @pytest.mark.asyncio
    async def test_a_turn_the_harness_did_not_keep_sends_its_lines_again(self):
        _turn_kept(fresh=True)
        first = await _fired(_wake("it_1"))
        held_blocks.begin_turn(KEY, fresh=False)
        held_blocks.note_wake(KEY, first)
        held_blocks.settle_turn(KEY, kept=False)

        again = await _fired(_wake("it_1"))

        assert again == first


class TestTheSeatsLine:
    @pytest.mark.asyncio
    async def test_it_is_sent_when_its_numbers_move_and_only_then(self):
        """Point 3, and the instruction under it once per context (point 2)."""
        _turn_kept(fresh=True)
        seats = _seats("it_aaaa1111")
        fact = seats.split("\n")[0]
        first = await _fired(_wake("it_1"), seats)
        assert fact in first and SEATS_HOW_TO in first
        _turn_kept(first)

        same = await _fired(_wake("it_2"), seats)
        assert "[seats]" not in same, "an unchanged seats line went out again"

        moved_seats = _seats("it_bbbb2222")
        moved = await _fired(_wake("it_3"), moved_seats)
        assert moved_seats.split("\n")[0] in moved
        assert SEATS_HOW_TO not in moved, "the instruction is held; only the fact moved"

    @pytest.mark.asyncio
    async def test_a_board_that_went_quiet_makes_the_same_line_news_again(self):
        _turn_kept(fresh=True)
        seats = _seats("it_aaaa1111")
        _turn_kept(await _fired(_wake("it_1"), seats))
        _turn_kept(await _fired(_wake("it_2"), ""))  # every seat had a worker

        back = await _fired(_wake("it_3"), seats)

        assert seats.split("\n")[0] in back

    @pytest.mark.asyncio
    async def test_a_steer_carries_the_line_by_the_same_rule(self):
        _turn_kept(fresh=True)
        seats = _seats("it_aaaa1111")
        _turn_kept(await _fired(_wake("it_1"), seats))

        assert "[seats]" not in await _steered(_wake("it_2"), seats)
        moved = _seats("it_bbbb2222")
        assert (await _steered(_wake("it_3"), moved)).endswith(moved.split("\n")[0])


class TestWakesThatWaitedTogether:
    def _bundle(self, *contents: str) -> str:
        from kiro_crew.dashboard.chat_utils_injected_rows import (
            AUTOMATION_STEER_META,
            _dequeue_agent_run,
        )

        slot = SimpleNamespace(
            key=CONDUCTOR,
            linked_session_key="",
            agent="",
            agent_kind="",
            mode="",
            _queue=[
                {"content": text, "meta": {AUTOMATION_STEER_META: {"loop_id": "loop-wl"}}}
                for text in contents
            ],
        )
        with patch("kiro_crew.conductor_step.is_conductor_slot", return_value=True):
            run = _dequeue_agent_run(slot)
        assert run is not None
        return run[0]

    def test_a_bundle_carries_each_fixed_line_once_and_the_newest_seats_line(self):
        """Point 2: four requeued wakes are not four copies of every line."""
        old, new = _seats("it_aaaa1111"), _seats("it_bbbb2222")
        wakes = [f"{_wake(f'it_{n}')}\n\n{old if n < 4 else new}" for n in range(1, 5)]

        prompt = self._bundle(*wakes)

        for n in range(1, 5):
            assert f"item=it_{n} status=done" in prompt, "every wake's news is kept"
        for block in (SUFFIX, POINTER, SEATS_HOW_TO):
            assert prompt.count(block) == 1
        assert old.split("\n")[0] not in prompt
        assert prompt.count(new.split("\n")[0]) == 1

    def test_a_chat_that_is_no_conductor_gets_the_bundle_it_always_got(self):
        from kiro_crew.dashboard.chat_utils_injected_rows import (
            AGENT_RUN_META,
            AUTOMATION_STEER_META,
            _dequeue_agent_run,
            agent_run_prompt,
        )

        wakes = [f"{_wake('it_1')}\n\n{_seats('it_aaaa1111')}"] * 2
        slot = SimpleNamespace(
            key="chat-plain",
            linked_session_key="",
            agent="",
            agent_kind="",
            mode="",
            _queue=[
                {"content": text, "meta": {AUTOMATION_STEER_META: {"loop_id": "loop-wl"}}}
                for text in wakes
            ],
        )
        run = _dequeue_agent_run(slot)
        assert run is not None
        assert run[0] == agent_run_prompt(wakes, run[1][0]["meta"][AGENT_RUN_META])


class TestWhatEndsAContext:
    @pytest.mark.asyncio
    async def test_a_compaction_sends_every_line_again_once(self):
        """A compaction Crew ran or saw in a turn ends in ``mark_needs_reinjection``."""
        from kiro_crew.session_parts import turn_context

        _turn_kept(await _fired(_wake("it_1"), _seats("it_aaaa1111")), fresh=True)
        manager = SimpleNamespace(_fold_key=lambda key: key, _compaction=MagicMock())

        turn_context.mark_needs_reinjection(manager, KEY)

        seats = _seats("it_aaaa1111")
        after = await _fired(_wake("it_2"), seats)
        for block in (SUFFIX, POINTER, PATROL, seats):
            assert after.count(block) == 1
        _turn_kept(after, fresh=True)
        assert SUFFIX not in await _fired(_wake("it_3"), seats), "once, not from now on"

    @pytest.mark.asyncio
    async def test_a_steer_after_a_compaction_in_the_same_turn_is_whole(self):
        from kiro_crew.session_parts import turn_context

        _turn_kept(await _fired(_wake("it_1")), fresh=True)
        manager = SimpleNamespace(_fold_key=lambda key: key, _compaction=MagicMock())
        turn_context.mark_needs_reinjection(manager, KEY)

        assert await _steered(_wake("it_2")) == _wake("it_2")

    @pytest.mark.asyncio
    async def test_a_wake_shortened_for_a_context_that_is_gone_gets_its_lines_back(self):
        """The fire cannot know a turn will open on an empty context; the build can."""
        from kiro_crew.dashboard.held_context import held_open_for_turn

        _turn_kept(await _fired(_wake("it_1")), fresh=True)
        short = await _fired(_wake("it_2"))
        assert POINTER not in short
        turn = SimpleNamespace(
            session_key=KEY,
            slot=_slot(),
            client=SimpleNamespace(reports_compaction=True),
            _directive_self_wake=True,
        )
        with patch("kiro_crew.dashboard.held_context.is_conductor_slot", return_value=True):
            owed = held_open_for_turn(
                turn, context_is_new=True, provider_has_history=False, needs_reinjection=False
            )
            assert owed.split("\n") == [SUFFIX, POINTER, PATROL]
            again = held_open_for_turn(
                turn, context_is_new=False, provider_has_history=True, needs_reinjection=False
            )
        assert again == "", "owed once"

    def test_only_a_conductor_on_a_harness_whose_compaction_crew_sees_has_a_record(self):
        from kiro_crew.dashboard.held_context import held_open_for_turn

        def _opened(*, conductor: bool, reports: object) -> bool:
            held_blocks.drop(KEY)
            turn = SimpleNamespace(
                session_key=KEY,
                slot=_slot(),
                client=SimpleNamespace(reports_compaction=reports),
                _directive_self_wake=False,
            )
            with patch(
                "kiro_crew.dashboard.held_context.is_conductor_slot", return_value=conductor
            ):
                held_open_for_turn(
                    turn, context_is_new=True, provider_has_history=False, needs_reinjection=False
                )
            return held_blocks.tracked(KEY)

        assert _opened(conductor=True, reports=True) is True
        assert _opened(conductor=False, reports=True) is False
        assert _opened(conductor=True, reports=False) is False
        assert _opened(conductor=True, reports="yes") is False, "H6: only a provider that says so"


class TestAChatWithNoRecord:
    @pytest.mark.asyncio
    async def test_its_fired_wake_is_the_whole_text_every_time(self):
        seats = _seats("it_aaaa1111")
        whole = f"{_wake('it_1')}\n\n[auto-nudge cycle 1]\n{PATROL}\n\n{seats}"

        assert await _fired(_wake("it_1"), seats) == whole
        assert await _fired(_wake("it_1"), seats) == whole

    @pytest.mark.asyncio
    async def test_its_steered_wake_is_the_whole_text_every_time(self):
        seats = _seats("it_aaaa1111")
        whole = f"{_wake('it_1')}\n\n{seats}"

        assert await _steered(_wake("it_1"), seats) == whole
        assert await _steered(_wake("it_1"), seats) == whole

    def test_its_turn_starts_are_what_they_were(self, tmp_path):
        """With no record, the build takes no branch the record guards."""
        builder = _builder(tmp_path)
        kinds = (
            {"is_new_session": True},
            {"is_new_session": True, "resumed": True},
            {"is_new_session": False},
            {"is_new_session": False, "needs_reinjection": True},
        )
        asked: list[tuple[str, str]] = []

        def _never_held(key, name, text):
            asked.append((key, name))
            return False

        for kind in kinds:
            plain = _built(builder, **kind)
            with (
                patch.object(held_blocks, "holds", _never_held),
                patch.object(held_blocks, "tracked", return_value=False),
                patch.object(held_blocks, "note", side_effect=AssertionError("no record")),
            ):
                assert _built(builder, **kind) == plain
            assert RESUMED in plain if kind.get("resumed") else RESUMED not in plain
            assert REPLY_RULES in plain
        assert asked, "the control: the build did ask"
        assert not held_blocks.tracked(KEY)


# ── the turn start ─────────────────────────────────────────────────────────


def _builder(tmp_path) -> ContextBuilder:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dashboard": {"verbosity": "concise"}}), encoding="utf-8")
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )


def _built(builder: ContextBuilder, **kind: bool) -> str:
    message, _ = builder.build_message(
        "wake", session_key=KEY, runtime_source="dashboard", interactive=True, **kind
    )
    return message


def _begin(*, fresh: bool) -> None:
    if held_blocks is not None:
        held_blocks.begin_turn(KEY, fresh=fresh)


def _end(*, kept: bool) -> None:
    if held_blocks is not None:
        held_blocks.settle_turn(KEY, kept=kept)


def _folded(text: str) -> str:
    return text.translate(ctx._MULTIBYTE_TABLE)


class TestAResumedTurn:
    def _blocks(self) -> tuple[str, str]:
        rules = _folded(ctx._critical_rules_for(KEY, "dashboard"))
        prefs = _folded(ctx._RESPONSE_PREFERENCES_HEADER)
        return rules, prefs

    def test_the_header_and_the_rules_go_out_once_per_context(self, tmp_path):
        """Point 4: a Conductor is resumed after every Stop and every idle hour."""
        builder = _builder(tmp_path)
        rules, prefs = self._blocks()
        _begin(fresh=True)
        start = _built(builder, is_new_session=True)
        for block in (rules, prefs, REPLY_RULES):
            assert start.count(block) == 1, "the control: a session start carries it"
        _end(kept=True)

        _begin(fresh=False)
        resumed = _built(builder, is_new_session=True, resumed=True)

        for block in (RESUMED, rules, prefs, REPLY_RULES):
            assert block not in resumed, "a block this context holds went out again"
        assert "[CURRENT DATE]" in resumed, "the date and identity are still refreshed"
        assert "[CURRENT USER REQUEST" in resumed

    def test_a_resume_restates_the_reply_language_in_place_of_the_preferences(self, tmp_path):
        path = config_path()
        builder = _builder(tmp_path)
        path.write_text(
            json.dumps({"dashboard": {"verbosity": "concise", "reply_language": "French"}}),
            encoding="utf-8",
        )
        from kiro_crew.context_assembly import sections

        refresh = _folded(sections.reply_language_refresh(KiroCrewConfig.load()))
        if not refresh:
            pytest.skip("this configuration states no reply language")
        _begin(fresh=True)
        _built(builder, is_new_session=True)
        _end(kept=True)
        _begin(fresh=False)

        assert refresh in _built(builder, is_new_session=True, resumed=True)

    def test_a_first_resume_after_a_restart_carries_them_once(self, tmp_path):
        """The record is in memory: with nothing known, the blocks go out."""
        builder = _builder(tmp_path)
        rules, _prefs = self._blocks()
        _begin(fresh=False)
        first = _built(builder, is_new_session=True, resumed=True)
        assert RESUMED in first and rules in first
        _end(kept=True)
        _begin(fresh=False)

        assert RESUMED not in _built(builder, is_new_session=True, resumed=True)

    def test_a_turn_the_harness_did_not_keep_does_not_count(self, tmp_path):
        builder = _builder(tmp_path)
        _begin(fresh=True)
        _built(builder, is_new_session=True)
        _end(kept=False)
        _begin(fresh=False)

        assert RESUMED in _built(builder, is_new_session=True, resumed=True)

    def test_a_compaction_brings_them_back(self, tmp_path):
        builder = _builder(tmp_path)
        rules, _prefs = self._blocks()
        _begin(fresh=True)
        _built(builder, is_new_session=True)
        _end(kept=True)
        held_blocks.drop(KEY)  # what mark_needs_reinjection does
        _begin(fresh=True)

        resumed = _built(builder, is_new_session=True, resumed=True)

        assert RESUMED in resumed and rules in resumed and REPLY_RULES in resumed

    def test_the_reply_rules_are_not_sent_again_on_a_warm_turn(self, tmp_path):
        """With no provider at all to take them out at the wire."""
        builder = _builder(tmp_path)
        _begin(fresh=True)
        _built(builder, is_new_session=True)
        _end(kept=True)
        _begin(fresh=False)

        assert REPLY_RULES not in _built(builder, is_new_session=False)

    def test_a_chat_that_alternates_typed_turns_and_wakes_gets_each_wording_once(self, tmp_path):
        """A typed turn's reply rules carry one paragraph a wake's do not.

        Seen live: with one wording remembered, every switch between a wake and a
        typed message sent the rules again.
        """
        builder = _builder(tmp_path)
        _begin(fresh=True)
        _built(builder, is_new_session=True, directive_user_origin=True)
        _end(kept=True)
        _begin(fresh=False)
        _built(builder, is_new_session=False)  # a wake: its own wording, once
        _end(kept=True)

        for _ in range(2):
            for typed in (True, False):
                _begin(fresh=False)
                later = _built(builder, is_new_session=False, directive_user_origin=typed)
                _end(kept=True)
                assert REPLY_RULES not in later, "a wording this context holds went out again"

    def test_a_build_that_outlives_its_stopped_turn_records_nothing(self, tmp_path):
        """A build runs on a worker and goes on after a Stop ended its turn.

        Seen live: a Stop 0.3 s into a turn, the next message sent at once. The
        stopped turn's build finished inside the next turn, recorded rules that
        were never sent, and the next message went out without them.
        """
        from kiro_crew.context_assembly import turn_message

        builder = _builder(tmp_path)
        resolve = turn_message._member.resolve_turn_owner

        def stopped_while_building(*args, **kwargs):
            _end(kept=False)
            _begin(fresh=True)  # the next message's turn
            return resolve(*args, **kwargs)

        _begin(fresh=True)
        with patch.object(turn_message._member, "resolve_turn_owner", stopped_while_building):
            _built(builder, is_new_session=False)

        assert REPLY_RULES in _built(
            builder, is_new_session=False
        ), "the next message left out rules that no message had carried"


# ── a Stop on a harness that keeps the stopped prompt ──────────────────────


def _settled_turn(*, claude: bool, terminal: bool = True, synthetic: bool = False, **over):
    """A turn at its end: it consumed the after-compaction re-send and was stopped."""
    sessions = MagicMock()
    turn = SimpleNamespace(
        session_key=KEY,
        slot=_slot(),
        state=SimpleNamespace(sessions=sessions, context_builder=None),
        client=SimpleNamespace(
            is_claude_backend=claude, is_kiro_backend=not claude, context_incarnation=("c", 1)
        ),
        _admission_slot=None,
        _drained_subagent_notices=[],
        _replay_accepted_this_turn=False,
        _responsive_recovery=[],
        _responsive_turn=SimpleNamespace(recovery=[]),
        _needs_reinjection=True,
        _member_session_start_pending=False,
        _turn_landed=False,
        _stop_reason="cancelled",
        _saw_terminal_event=terminal,
        _terminal_synthetic=synthetic,
        _terminal_reported_usage=True,
        needs_conversation_discard=False,
        _first_turn_history_assembled=True,
        _compaction_completed=False,
        _had_empty_response_verdict=False,
        _recovering_compaction=False,
        _turn_emitted=False,
    )
    for name, value in over.items():
        setattr(turn, name, value)
    return turn, sessions


#: The Claude adapter's two answers to a stopped prompt (claude-agent-acp 0.109.0).
#: A prompt Claude Code had started is answered with the turn's token counts, zero
#: when the stop came before the model answered. One still waiting in Claude Code's
#: queue is answered with the stop reason alone, and the adapter then removes it
#: from the queue: it never reaches the conversation.
_STARTED_THEN_STOPPED = {
    "stopReason": "cancelled",
    "usage": {
        "inputTokens": 0,
        "outputTokens": 0,
        "cachedReadTokens": 0,
        "cachedWriteTokens": 0,
        "totalTokens": 0,
    },
}
_STOPPED_IN_THE_QUEUE = {"stopReason": "cancelled"}


async def _stopped_claude_turn(answer: dict):
    """A stopped turn at its end, as Crew's Claude client and provider read ``answer``."""
    client = AcpClient(acp_backend="claude")

    async def answered(req_id, timeout):
        yield "complete", JsonRpcMessage(id=req_id, result=answer)

    client._prompt_loop = answered
    events = [_to_llm_event(ev) async for ev in client._dispatch_events(req_id=1, timeout=5.0)]
    terminal = events[-1]
    assert (terminal.kind, terminal.stop_reason) == (EVENT_COMPLETE, "cancelled")
    held_blocks.begin_turn(KEY, fresh=True)
    held_blocks.note_wake(KEY, _wake("it_1"))
    return _settled_turn(
        claude=True,
        synthetic=terminal.synthetic_completion,
        # Read as the turn's end reads it; a tree without the field has no such fact.
        _terminal_reported_usage=getattr(terminal, "answer_reported_usage", None),
    )


class TestAStoppedPromptTheHarnessKept:
    @pytest.mark.asyncio
    async def test_a_prompt_stopped_in_claude_codes_queue_still_owes_everything(self):
        """Claude Code never read it, so nothing it carried is in the conversation.

        Counted as kept, the after-compaction re-send, the first-turn history and
        the fixed blocks were never sent again in that context.
        """
        turn, sessions = await _stopped_claude_turn(_STOPPED_IN_THE_QUEUE)

        await chat_runner._turn_settle_debts(turn)

        sessions.mark_needs_reinjection.assert_called_once_with(KEY)
        sessions.consume_first_turn_history_owed.assert_not_called()
        assert not held_blocks.holds(KEY, held_blocks.WAKE_READ_POINTER, POINTER)

    @pytest.mark.asyncio
    async def test_a_prompt_claude_code_had_started_is_kept_when_stopped(self):
        """The control: stopped before the model answered, its text is in the conversation."""
        turn, sessions = await _stopped_claude_turn(_STARTED_THEN_STOPPED)

        await chat_runner._turn_settle_debts(turn)

        sessions.mark_needs_reinjection.assert_not_called()
        sessions.consume_first_turn_history_owed.assert_called_once_with(KEY)
        assert held_blocks.holds(KEY, held_blocks.WAKE_READ_POINTER, POINTER)

    @pytest.mark.asyncio
    async def test_a_stop_on_claude_does_not_owe_the_re_send_again(self):
        """Point 1: seven messages behind a Stop each carried the whole re-send."""
        turn, sessions = _settled_turn(claude=True)

        await chat_runner._turn_settle_debts(turn)

        sessions.mark_needs_reinjection.assert_not_called()
        sessions.consume_first_turn_history_owed.assert_called_once_with(KEY)

    @pytest.mark.asyncio
    async def test_a_stop_on_kiro_cli_still_owes_it(self):
        """kiro-cli writes a prompt only once the model answered it."""
        turn, sessions = _settled_turn(claude=False)

        await chat_runner._turn_settle_debts(turn)

        sessions.mark_needs_reinjection.assert_called_once_with(KEY)
        sessions.consume_first_turn_history_owed.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "over",
        [
            {"_saw_terminal_event": False},
            {"_terminal_synthetic": True},
            {"_terminal_reported_usage": False},
            {"needs_conversation_discard": True},
            {"_stop_reason": "error: backend"},
            {"_member_session_start_pending": True},
        ],
        ids=[
            "no-terminal",
            "made-up-terminal",
            "not-started",
            "discarded",
            "failed",
            "member-start",
        ],
    )
    async def test_a_claude_turn_the_harness_may_not_hold_still_owes_it(self, over):
        turn, sessions = _settled_turn(claude=True, **over)

        await chat_runner._turn_settle_debts(turn)

        sessions.mark_needs_reinjection.assert_called_once_with(KEY)

    @pytest.mark.asyncio
    async def test_the_record_keeps_what_a_stopped_claude_prompt_carried(self):
        held_blocks.begin_turn(KEY, fresh=True)
        held_blocks.note_wake(KEY, _wake("it_1"))
        turn, _sessions = _settled_turn(claude=True, _needs_reinjection=False)

        await chat_runner._turn_settle_debts(turn)

        assert held_blocks.holds(KEY, held_blocks.WAKE_READ_POINTER, POINTER)

    @pytest.mark.asyncio
    async def test_the_record_drops_what_a_stopped_kiro_prompt_carried(self):
        held_blocks.begin_turn(KEY, fresh=True)
        held_blocks.note_wake(KEY, _wake("it_1"))
        turn, _sessions = _settled_turn(claude=False, _needs_reinjection=False)

        await chat_runner._turn_settle_debts(turn)

        assert not held_blocks.holds(KEY, held_blocks.WAKE_READ_POINTER, POINTER)


class TestTheRecord:
    def test_a_line_a_person_wrote_is_left_alone(self):
        text = "please read the ledger\n\n[seats]are a thing I typed\nThe watch stays armed."
        held_blocks.begin_turn(KEY, fresh=True)

        assert held_blocks.lean_wake(KEY, text) is text

    def test_the_oldest_session_is_forgotten_past_the_bound(self, monkeypatch):
        monkeypatch.setattr(held_blocks, "MAX_SESSIONS", 2)
        keys = [f"dashboard:bound-{n}" for n in range(3)]
        try:
            for key in keys:
                held_blocks.begin_turn(key, fresh=True)
            assert [held_blocks.tracked(key) for key in keys] == [False, True, True]
        finally:
            for key in keys:
                held_blocks.drop(key)

    def test_every_wording_of_a_turn_start_block_is_held(self):
        held_blocks.begin_turn(KEY, fresh=True)
        held_blocks.note(KEY, {held_blocks.REPLY_FORMAT_RULES: "typed wording"})
        held_blocks.note(KEY, {held_blocks.REPLY_FORMAT_RULES: "wake wording"})
        held_blocks.settle_turn(KEY, kept=True)

        assert held_blocks.holds(KEY, held_blocks.REPLY_FORMAT_RULES, "typed wording")
        assert held_blocks.holds(KEY, held_blocks.REPLY_FORMAT_RULES, "wake wording")
        assert not held_blocks.holds(KEY, held_blocks.REPLY_FORMAT_RULES, "a third")

    def test_a_note_from_a_turn_that_is_over_is_not_taken(self):
        held_blocks.begin_turn(KEY, fresh=True)
        stopped = held_blocks.turn_of(KEY)
        held_blocks.settle_turn(KEY, kept=False)
        held_blocks.begin_turn(KEY, fresh=True)

        held_blocks.note(KEY, {held_blocks.CRITICAL_RULES: "rules"}, turn=stopped)
        assert not held_blocks.holds(KEY, held_blocks.CRITICAL_RULES, "rules")

        held_blocks.note(KEY, {held_blocks.CRITICAL_RULES: "rules"}, turn=held_blocks.turn_of(KEY))
        assert held_blocks.holds(KEY, held_blocks.CRITICAL_RULES, "rules")
        assert held_blocks.turn_of("dashboard:never-opened") == 0

    def test_a_key_that_is_no_session_has_no_record(self):
        held_blocks.begin_turn(MagicMock(), fresh=True)
        assert held_blocks.lean_wake(MagicMock(), _wake("it_1")) == _wake("it_1")
        assert not held_blocks.tracked("")


# ── the session-ledger snapshot of a fired wake ────────────────────────────

CHANGES = getattr(held_blocks, "LEDGER_CHANGES_HEADER", "<no changes header in this tree>")
REMOVED = getattr(held_blocks, "LEDGER_ROW_REMOVED", "<no removed line in this tree>")
CUT_OFF = getattr(held_blocks, "LEDGER_ROW_CUT_OFF", "<no cut-off line in this tree>")
NEWS_ONLY = "[work-ledger wake] item={} status=done\n\n[auto-nudge cycle 1]"


def _ledger(*, next_step: str = "merge pr-7 once its checks are green", **over) -> dict:
    """A made-up session ledger, in the shape ``session_ledger.read_state`` returns."""
    state = {
        "goal": "ship the wake fix and keep the board moving",
        "phase": "executing",
        "next": next_step,
        "tried": [{"approach": "one wide worker", "rejected_because": "too slow"}],
        "artifacts": {"pr-7": "open, checks green", "worker": "chat-12 holds it_7"},
    }
    state.update(over)
    return state


def _snapshot(state: dict) -> str:
    from kiro_crew import session_ledger

    with patch.object(session_ledger, "read_state", return_value=state):
        return session_ledger.render_snapshot("chat-conductor")


async def _fired_on(state: dict, item: str = "it_1") -> str:
    """The prompt one fire starts an idle Conductor's turn with, on this ledger."""
    from kiro_crew import session_ledger

    with patch.object(session_ledger, "read_state", return_value=state):
        return await _fired(_wake(item))


def _whole(state: dict, item: str = "it_1") -> str:
    """A fired wake as every chat without a record gets it."""
    return f"{_wake(item)}\n\n[auto-nudge cycle 1]\n{_snapshot(state)}\n\n{PATROL}"


def _no_ledger_block(prompt: str) -> bool:
    """Whether a fired wake carries neither the snapshot nor a changes block."""
    return _snapshot(_ledger()).split("\n")[0] not in prompt and CHANGES not in prompt


def _wake_turn(
    prompt: str,
    *,
    fresh: bool = False,
    kept: bool = True,
    around: str = "{}",
    own: bool = True,
    gate: bool = True,
) -> str:
    """The build and the end of the turn a fire started with *prompt*; the lines owed it.

    The route a real turn takes: ``held_open_for_turn``, ``held_note_prompt`` and the
    settle. *around* is what a build may put around the wake before it gets there.
    *own* False is the retry of a wake turn that failed: the same text from the
    queue, in a turn no fire started. *gate* False is a chat that is no Conductor's
    by the time the turn is built.
    """
    import inspect

    from kiro_crew.dashboard.held_context import held_note_prompt, held_open_for_turn

    turn = SimpleNamespace(
        session_key=KEY,
        slot=_slot(),
        client=SimpleNamespace(reports_compaction=True),
        _directive_self_wake=own,
    )
    text = around.format(prompt)
    takes_text = "wake_text" in inspect.signature(held_open_for_turn).parameters
    with patch("kiro_crew.dashboard.held_context.is_conductor_slot", return_value=gate):
        owed = held_open_for_turn(
            turn,
            context_is_new=fresh,
            provider_has_history=False,
            needs_reinjection=False,
            **({"wake_text": text} if takes_text else {}),
        )
    held_note_prompt(turn, owed, text)
    held_blocks.settle_turn(KEY, kept=kept)
    return owed


class TestTheLedgerSnapshot:
    """Point 5: the snapshot rode every wake whole, though no row had moved."""

    @pytest.mark.asyncio
    async def test_a_context_gets_it_whole_once_and_nothing_while_no_row_moves(self):
        state = _ledger()
        first = await _fired_on(state)
        assert first == _whole(state), "the first wake of a context carries every row"
        _wake_turn(first, fresh=True)

        later = await _fired_on(state, "it_2")

        assert later == NEWS_ONLY.format("it_2")

    @pytest.mark.asyncio
    async def test_a_changed_row_goes_out_under_a_line_that_says_the_rest_stands(self):
        _wake_turn(await _fired_on(_ledger()), fresh=True)

        later = await _fired_on(_ledger(next_step="rebase pr-7"), "it_2")

        assert later == f"{NEWS_ONLY.format('it_2')}\n{CHANGES}\nnext: rebase pr-7"
        assert "still stands" in CHANGES and "session_ledger_read" in CHANGES
        assert CHANGES != _snapshot(_ledger()).split("\n")[0], "a part must not read as the whole"

    @pytest.mark.asyncio
    async def test_a_new_row_and_a_changed_artifact_go_out_and_no_other(self):
        _wake_turn(await _fired_on(_ledger()), fresh=True)
        moved = _ledger(
            artifacts={
                "worker": "chat-12 holds it_7",
                "pr-7": "merged",
                "pr-9": "open, review asked",
            }
        )

        later = await _fired_on(moved, "it_2")

        assert later.split("\n")[3:] == [
            CHANGES,
            "artifact pr-7: merged",
            "artifact pr-9: open, review asked",
        ]

    @pytest.mark.asyncio
    async def test_a_removed_row_is_said_as_removed(self):
        _wake_turn(await _fired_on(_ledger()), fresh=True)

        later = await _fired_on(_ledger(artifacts={"pr-7": "open, checks green"}), "it_2")

        assert later.split("\n")[3:] == [CHANGES, f"{REMOVED}artifact worker"]

    @pytest.mark.asyncio
    async def test_a_tried_row_is_an_entry_added_never_one_removed(self):
        """The snapshot shows the newest three; one that left the view was still tried."""
        tried = [{"approach": f"approach {n}", "rejected_because": ""} for n in range(1, 5)]
        _wake_turn(await _fired_on(_ledger(tried=tried[:3])), fresh=True)

        later = await _fired_on(_ledger(tried=tried), "it_2")

        assert later.split("\n")[3:] == [CHANGES, "tried: approach 4"]

    @pytest.mark.asyncio
    async def test_the_changes_a_turn_kept_are_held_and_a_row_that_moves_back_goes_out(self):
        _wake_turn(await _fired_on(_ledger()), fresh=True)
        _wake_turn(await _fired_on(_ledger(next_step="rebase pr-7"), "it_2"))

        assert await _fired_on(_ledger(next_step="rebase pr-7"), "it_3") == NEWS_ONLY.format("it_3")
        back = await _fired_on(_ledger(), "it_4")
        assert back.split("\n")[3:] == [CHANGES, "next: merge pr-7 once its checks are green"]

    @pytest.mark.asyncio
    async def test_a_removal_a_turn_kept_is_held(self):
        _wake_turn(await _fired_on(_ledger()), fresh=True)
        gone = _ledger(artifacts={"pr-7": "open, checks green"})
        _wake_turn(await _fired_on(gone, "it_2"))

        assert await _fired_on(gone, "it_3") == NEWS_ONLY.format("it_3"), "said removed twice"
        again = await _fired_on(_ledger(), "it_4")
        assert again.split("\n")[3:] == [CHANGES, "artifact worker: chat-12 holds it_7"]

    @pytest.mark.asyncio
    async def test_a_wake_that_fired_and_started_no_turn_leaves_the_record_as_it_was(self):
        """What a context holds is what a turn kept, never what a fire composed."""
        _wake_turn(await _fired_on(_ledger()), fresh=True)
        moved = _ledger(next_step="rebase pr-7")

        unsent = await _fired_on(moved, "it_2")  # no turn was built for it
        again = await _fired_on(moved, "it_2")

        assert again == unsent, "a wake no turn took changed what the next one says"
        assert again.split("\n")[3:] == [CHANGES, "next: rebase pr-7"]

    @pytest.mark.asyncio
    async def test_a_turn_the_harness_did_not_keep_sends_its_rows_again(self):
        first = await _fired_on(_ledger())
        _wake_turn(first, fresh=True, kept=False)

        assert await _fired_on(_ledger()) == first

        _wake_turn(first)
        changes = await _fired_on(_ledger(next_step="rebase pr-7"), "it_2")
        assert CHANGES in changes
        _wake_turn(changes, kept=False)

        assert await _fired_on(_ledger(next_step="rebase pr-7"), "it_2") == changes

    @pytest.mark.asyncio
    async def test_a_compaction_sends_it_whole_again_once(self):
        from kiro_crew.session_parts import turn_context

        state = _ledger()
        _wake_turn(await _fired_on(state), fresh=True)
        manager = SimpleNamespace(_fold_key=lambda key: key, _compaction=MagicMock())

        turn_context.mark_needs_reinjection(manager, KEY)

        after = await _fired_on(state, "it_2")
        assert after == _whole(state, "it_2")
        _wake_turn(after, fresh=True)
        assert await _fired_on(state, "it_3") == NEWS_ONLY.format("it_3"), "once, not from now on"

    @pytest.mark.asyncio
    async def test_a_restart_sends_it_whole_again_once(self):
        """The record is in memory: a new process knows nothing a context holds."""
        state = _ledger()
        _wake_turn(await _fired_on(state), fresh=True)

        held_blocks._records.clear()

        after = await _fired_on(state, "it_2")
        assert after == _whole(state, "it_2")
        _wake_turn(after)  # the resumed conversation: the record starts from this turn
        assert await _fired_on(state, "it_3") == NEWS_ONLY.format("it_3")

    @pytest.mark.asyncio
    async def test_a_wake_shortened_for_a_context_that_is_gone_gets_the_whole_block_back(self):
        """A new conversation: the fire cannot know, the build can."""
        _wake_turn(await _fired_on(_ledger()), fresh=True)
        moved = _ledger(next_step="rebase pr-7")
        short = await _fired_on(moved, "it_2")
        assert _snapshot(moved) not in short

        owed = _wake_turn(short, fresh=True)

        assert owed == "\n".join([SUFFIX, POINTER, PATROL, "", _snapshot(moved)])
        assert await _fired_on(moved, "it_3") == NEWS_ONLY.format(
            "it_3"
        ), "the new context holds it"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("first_context_was_new", [False, True])
    async def test_a_shortened_wake_retried_into_a_new_context_gets_the_whole_block_back(
        self, first_context_was_new
    ):
        """The wake turn failed; its retry is the same text in a turn no fire started."""
        _wake_turn(await _fired_on(_ledger()), fresh=True)
        moved = _ledger(next_step="rebase pr-7")
        short = await _fired_on(moved, "it_2")
        assert _snapshot(moved) not in short
        _wake_turn(short, fresh=first_context_was_new, kept=False)

        owed = _wake_turn(short, fresh=True, own=False)

        assert owed == _snapshot(moved)
        assert _no_ledger_block(await _fired_on(moved, "it_3")), "the new context holds it"

    @pytest.mark.asyncio
    async def test_a_retried_wake_is_recorded_as_the_turn_the_fire_started_would_be(self):
        """The same conversation: nothing is owed, and what the retry carried is held."""
        _wake_turn(await _fired_on(_ledger()), fresh=True)
        moved = _ledger(next_step="rebase pr-7")
        short = await _fired_on(moved, "it_2")
        _wake_turn(short, kept=False)

        assert _wake_turn(short, own=False) == ""

        assert await _fired_on(moved, "it_3") == NEWS_ONLY.format("it_3"), "the row went out twice"

    @pytest.mark.asyncio
    async def test_a_compaction_inside_the_failed_wake_turn_keeps_the_block_owed_its_retry(self):
        _wake_turn(await _fired_on(_ledger()), fresh=True)
        moved = _ledger(next_step="rebase pr-7")
        short = await _fired_on(moved, "it_2")
        _wake_turn(short, kept=False)
        held_blocks.drop(KEY)  # what mark_needs_reinjection does

        assert _wake_turn(short, fresh=True, own=False) == _snapshot(moved)

    @pytest.mark.asyncio
    async def test_a_chat_that_left_the_gate_after_a_shortened_fire_gets_the_whole_block_back(
        self,
    ):
        """Moved to another harness, or no Conductor any more, between the fire and the build."""
        _wake_turn(await _fired_on(_ledger()), fresh=True)
        moved = _ledger(next_step="rebase pr-7")
        short = await _fired_on(moved, "it_2")
        assert _snapshot(moved) not in short

        owed = _wake_turn(short, gate=False, kept=False)

        assert owed == "\n".join([SUFFIX, POINTER, PATROL, "", _snapshot(moved)])
        assert not held_blocks.tracked(KEY)
        assert _wake_turn(short, gate=False, own=False) == _snapshot(moved), "its retry"
        assert await _fired_on(moved, "it_3") == _whole(moved, "it_3")

    @pytest.mark.asyncio
    async def test_the_block_owed_to_a_compacted_context_is_not_sent_beside_the_re_send(
        self, tmp_path
    ):
        """A compaction flagged late: the owed block and the re-send are one snapshot."""
        from kiro_crew import session_ledger

        state = _ledger()
        builder = _builder(tmp_path)
        _begin(fresh=True)
        with patch.object(session_ledger, "read_state", return_value=state):
            message, _ = builder.build_message(
                "[auto-nudge cycle 2]",
                is_new_session=False,
                session_key=KEY,
                runtime_source="dashboard",
                needs_reinjection=True,
                request_prefix_context=_snapshot(state),
            )

        assert message.count(_folded(_snapshot(state).split("\n")[0])) == 1

    @pytest.mark.asyncio
    async def test_the_wake_is_found_whatever_the_build_put_around_it(self):
        _wake_turn(await _fired_on(_ledger()), fresh=True)
        changes = await _fired_on(_ledger(next_step="rebase pr-7"), "it_2")

        _wake_turn(changes, around="[a notice the build put first]\n\n{}\n\n[Skill: x]")

        assert await _fired_on(_ledger(next_step="rebase pr-7"), "it_3") == NEWS_ONLY.format("it_3")

    @pytest.mark.asyncio
    async def test_a_snapshot_cut_at_its_ceiling_never_says_removed(self):
        """Past the cut a row is not shown; whether it is gone the snapshot cannot say."""
        wide = {f"pr-{n}": "open, waits on review " * 12 for n in range(1, 8)}
        state = _ledger(artifacts=wide)
        assert _snapshot(state).endswith("…"), "the control: this one is cut"
        _wake_turn(await _fired_on(state), fresh=True)
        shown = [row for row in _snapshot(state).split("\n") if row.startswith("artifact ")]
        longer = _ledger(next_step="rebase pr-7 " * 30, artifacts=wide)
        pushed_out = [
            row.split(": ")[0]
            for row in shown
            if row.split(": ")[0] + ": " not in _snapshot(longer)
        ]
        assert pushed_out, "the control: a longer row pushed one past the cut"

        later = await _fired_on(longer, "it_2")

        assert CHANGES in later and REMOVED not in later
        for label in pushed_out:
            assert f"{CUT_OFF}{label}" in later

    @pytest.mark.asyncio
    async def test_changes_no_shorter_than_the_block_go_out_as_the_block(self):
        small = _ledger(goal="", phase="", tried=[], artifacts={}, next_step="a")
        _wake_turn(await _fired_on(small), fresh=True)
        moved = _ledger(goal="", phase="", tried=[], artifacts={}, next_step="b")

        later = await _fired_on(moved, "it_2")

        assert later == f"{NEWS_ONLY.format('it_2')}\n{_snapshot(moved)}"
        _wake_turn(later)
        assert await _fired_on(moved, "it_3") == NEWS_ONLY.format("it_3"), "the whole block counts"

    @pytest.mark.asyncio
    async def test_a_chat_with_no_record_gets_the_whole_block_every_time(self):
        state = _ledger()

        assert await _fired_on(state) == _whole(state)
        assert await _fired_on(state) == _whole(state)
        assert not held_blocks.tracked(KEY), "a fire opened a record; only a build may"

    @pytest.mark.asyncio
    async def test_a_chat_that_is_no_conductor_never_gets_a_record_from_a_fire(self):
        from kiro_crew.dashboard.held_context import held_note_prompt, held_open_for_turn

        state = _ledger()
        first = await _fired_on(state)
        turn = SimpleNamespace(
            session_key=KEY,
            slot=_slot(),
            client=SimpleNamespace(reports_compaction=True),
            _directive_self_wake=True,
        )
        with patch("kiro_crew.dashboard.held_context.is_conductor_slot", return_value=False):
            owed = held_open_for_turn(
                turn, context_is_new=True, provider_has_history=False, needs_reinjection=False
            )
        held_note_prompt(turn, owed, first)
        held_blocks.settle_turn(KEY, kept=True)

        assert owed == "" and KEY not in held_blocks._records
        assert await _fired_on(state, "it_2") == _whole(state, "it_2")


class TestWhereTheSnapshotRides:
    """Only the turn a fire starts carries it; nothing else is read for one."""

    @pytest.mark.asyncio
    async def test_steered_wakes_and_their_bundle_carry_none_and_move_no_row(self):
        """Wakes that reach a busy Conductor are steered, and wait as cards when not kept."""
        _wake_turn(await _fired_on(_ledger()), fresh=True)
        moved = _ledger(next_step="rebase pr-7")
        with patch("kiro_crew.session_ledger.read_state", return_value=moved):
            cards = [await _steered(_wake("it_2")), await _steered(_wake("it_3"))]

        prompt = TestWakesThatWaitedTogether()._bundle(*cards)
        _turn_kept(prompt)

        assert "[work ledger" not in prompt
        assert "item=it_2 status=done" in prompt and "item=it_3 status=done" in prompt
        later = await _fired_on(moved, "it_4")
        assert later.split("\n")[3:] == [CHANGES, "next: rebase pr-7"]

    @pytest.mark.asyncio
    async def test_a_steer_the_turn_read_never_counts_as_a_snapshot_sent(self):
        state = _ledger()
        _turn_kept(fresh=True)
        with patch("kiro_crew.session_ledger.read_state", return_value=state):
            steered = await _steered(_wake("it_1"))
        assert "[work ledger" not in steered
        held_blocks.steer_read(KEY, steered)
        held_blocks.settle_turn(KEY, kept=True)

        assert await _fired_on(state, "it_2") == (
            f"[work-ledger wake] item=it_2 status=done\n\n[auto-nudge cycle 1]\n"
            f"{_snapshot(state)}\n\n{PATROL}"
        )

    @pytest.mark.asyncio
    async def test_a_block_that_only_looks_like_one_is_neither_shortened_nor_recorded(self):
        """A monitor's evidence, a pasted message: text Crew did not compose as a snapshot."""
        state = _ledger()
        _wake_turn(await _fired_on(state), fresh=True)
        quoted = f"a comment on the pull request:\n{_snapshot(_ledger(next_step='do harm'))}\n\nend"

        assert held_blocks.lean_wake(KEY, quoted, starts_turn=True) == quoted
        assert held_blocks.lean_wake(KEY, quoted) == quoted
        assert held_blocks.one_copy([quoted, quoted]) == [quoted, quoted]
        _turn_kept(quoted)  # a typed turn, a queued message

        assert await _fired_on(state, "it_2") == NEWS_ONLY.format("it_2"), "the record moved"

    @pytest.mark.asyncio
    async def test_a_wake_turn_with_a_snapshot_no_fire_answers_for_makes_the_record_forget(self):
        """Never recorded from a text; and from then on the record vouches for no row."""
        state = _ledger()
        _wake_turn(await _fired_on(state), fresh=True)

        _wake_turn(f"[auto-nudge cycle 9]\n{CHANGES}\nnext: do harm")

        later = await _fired_on(state, "it_2")
        assert later == f"{NEWS_ONLY.format('it_2')}\n{_snapshot(state)}"

    @pytest.mark.asyncio
    async def test_a_fired_wake_crew_lost_track_of_makes_the_record_forget(self):
        state = _ledger()
        _wake_turn(await _fired_on(state), fresh=True)
        bound = getattr(held_blocks, "MAX_FIRED_SNAPSHOTS", 4)

        for n in range(bound + 1):  # each one started, none was built
            await _fired_on(_ledger(next_step=f"step {n}"), f"it_{n}")

        later = await _fired_on(state, "it_9")
        assert later == f"{NEWS_ONLY.format('it_9')}\n{_snapshot(state)}"


class TestTheProjectAndRuntimeLines:
    """Point 6: both rode nearly every turn of a context that already held them."""

    PROJECT = "[PROJECT] Active project directory: "
    RUNTIME = "[RUNTIME] "
    FOR_THIS_TURN = "For THIS turn: this surface renders no tool cards"

    def _turn(self, builder, *, project="/work/alpha", source="dashboard", kept=True, **kind):
        message, _ = builder.build_message(
            "wake",
            session_key=KEY,
            runtime_source=source,
            interactive=True,
            project=project,
            **({"is_new_session": False} | kind),
        )
        _end(kept=kept)
        _begin(fresh=False)
        return message

    def test_each_goes_out_once_per_context(self, tmp_path):
        builder = _builder(tmp_path)
        _begin(fresh=True)
        first = self._turn(builder)
        assert first.count(self.PROJECT + "/work/alpha") == 1 and first.count(self.RUNTIME) == 1

        later = self._turn(builder)

        assert self.PROJECT not in later and self.RUNTIME not in later
        assert "[CURRENT USER REQUEST" in later

    def test_a_session_start_counts_for_the_project_line(self, tmp_path):
        builder = _builder(tmp_path)
        _begin(fresh=True)
        assert self.PROJECT in self._turn(builder, is_new_session=True)

        assert self.PROJECT not in self._turn(builder)

    def test_a_changed_project_goes_out_and_the_runtime_line_does_not(self, tmp_path):
        builder = _builder(tmp_path)
        _begin(fresh=True)
        self._turn(builder)

        moved = self._turn(builder, project="/work/beta")

        assert self.PROJECT + "/work/beta" in moved and self.RUNTIME not in moved
        assert self.PROJECT not in self._turn(builder, project="/work/beta")
        assert self.PROJECT + "/work/alpha" in self._turn(builder), "back is a change too"

    def test_a_turn_another_interface_carries_always_names_itself(self, tmp_path):
        """A message typed in a linked Slack thread runs in the same chat."""
        builder = _builder(tmp_path)
        _begin(fresh=True)
        dashboard = self._turn(builder)
        self._turn(builder)

        for _ in range(2):
            slack = self._turn(builder, source="slack")
            assert self.RUNTIME in slack and self.FOR_THIS_TURN in slack
            assert self.PROJECT in slack
        back = self._turn(builder)

        runtime_line = next(row for row in dashboard.split("\n") if row.startswith(self.RUNTIME))
        assert runtime_line in back, "the context ended on Slack's line"
        assert self.FOR_THIS_TURN not in back
        assert self.RUNTIME not in self._turn(builder)

    def test_a_turn_the_harness_did_not_keep_does_not_count(self, tmp_path):
        builder = _builder(tmp_path)
        _begin(fresh=True)
        self._turn(builder, kept=False)

        again = self._turn(builder)

        assert self.PROJECT in again and self.RUNTIME in again
        assert self.PROJECT not in self._turn(builder), "kept this time"

    def test_a_compaction_brings_both_back(self, tmp_path):
        builder = _builder(tmp_path)
        _begin(fresh=True)
        self._turn(builder)
        assert self.PROJECT not in self._turn(builder), "the control: the context held it"
        held_blocks.drop(KEY)  # what mark_needs_reinjection does
        _begin(fresh=True)

        after = self._turn(builder, needs_reinjection=True)

        assert self.PROJECT in after and self.RUNTIME in after

    def test_a_chat_with_no_record_gets_both_on_every_turn(self, tmp_path):
        builder = _builder(tmp_path)
        turns = []
        for _ in range(2):
            message, _ = builder.build_message(
                "wake",
                session_key=KEY,
                runtime_source="dashboard",
                interactive=True,
                project="/work/alpha",
                is_new_session=False,
            )
            turns.append(message)

        assert turns[0] == turns[1]
        assert self.PROJECT in turns[1] and self.RUNTIME in turns[1]
