"""Whether a steer the running turn accepted is being held behind a tool call.

Some agents take a steer only between steps: one sent while a tool call of the
turn is open is accepted and held, and goes to the model when that call returns
(``AcpClient._steer_via_steering_request``). The sender's receipt then has to say
so, because "it reads it at its next step" can mean many minutes.

A read of the moment: a steer released between the accept and this read reports
no hold, which is by then the truth.
"""

from __future__ import annotations

from typing import Any


def held_behind_tool(slot: Any) -> bool:
    """True while *slot*'s agent holds an accepted steer behind an open tool call."""
    client = getattr(slot, "_acp_client", None)
    return getattr(client, "steer_held_behind_tool", False) is True
