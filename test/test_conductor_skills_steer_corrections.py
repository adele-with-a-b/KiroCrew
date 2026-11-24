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
