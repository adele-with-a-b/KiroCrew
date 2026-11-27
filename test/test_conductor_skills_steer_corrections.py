"""The conductor skills must tell the conductor its message reaches a busy worker now.

A ``session_send`` with no ``steer`` to a busy worker the conductor manages cuts
into its running turn (``session_control.send_to_target``), so a correction lands
at the worker's next step instead of after it has spent the whole turn going the
wrong way. These tests pin that the goal and pipeline conductor skills say so,
name ``steer: false`` as the only way to wait for the turn boundary, and warn
that a steer does not withdraw messages already queued.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew import ledger_wake
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

GOAL_SKILL = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "SKILL.md"
PIPELINE_SKILL = Path(_BUILTIN_SKILLS_DIR) / "pipeline-conductor" / "SKILL.md"


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _section(body: str, heading: str, next_heading: str) -> str:
    start = body.index(heading)
    end = body.index(next_heading, start + len(heading))
    return body[start:end]


def test_goal_conductor_steers_a_correction_into_a_running_item():
    section = _section(
        _flat(GOAL_SKILL), "### Goal changes mid-flight", "## When a conductor dispatched you"
    )
    assert "`session_send` the correction straight into it with `steer: true`" in section
    assert "**Your message reaches your worker now.**" in section
    # Only what must wait for the turn boundary asks for the queue by name.
    assert "Pass `steer: false` only for something that must wait for the turn boundary" in (
        section
    )
    # The reply comes back into the asking turn, and only `wait` can see it there.
    assert "`wait` returns early when the reply lands" in section
    # A steer is additive: what is already queued still runs.
    assert "A steer does not withdraw what is already queued" in section
    assert "`queue_depth`" in section


def test_pipeline_conductor_forwards_a_live_mode_change_as_a_steer():
    section = _section(_flat(PIPELINE_SKILL), "## Live steering", "## Merge, cleanup, reconcile")
    assert "a send with no `steer` lands in its running turn at its next step" in section
    assert "Pass `steer: false` only for something that must wait for the turn boundary" in (
        section
    )
    assert "`steer: true` for a busy session you do not manage" in section
    assert "A steer does not withdraw messages already queued" in section


def test_goal_conductor_resends_an_unconfirmed_message_only_when_its_turn_failed():
    """``reason=message_unconfirmed`` covers a failed turn (send again) and a turn
    still running (most likely read): the skill branches on the wake's ``turn=``,
    in the words the one writer of that wake emits."""
    section = _section(
        _flat(GOAL_SKILL),
        "A wake with `reason=message_unconfirmed`",
        "A wake with `reason=report_unread`",
    )
    assert "`turn=running`:" in section, "the skill names the case it must not resend"
    failed, running = section.split("`turn=running`:")
    assert "`turn=failed`:" in failed and "send again what still matters" in failed
    assert "Do not send it again on the wake alone" in running
    assert "send again only if that shows it missed the message" in running

    brief = ledger_wake.unconfirmed_wake_brief
    assert brief(item_id="it_1", count=1).endswith(" turn=failed")
    assert " turn=running" in brief(item_id="it_1", count=1, running=True)
