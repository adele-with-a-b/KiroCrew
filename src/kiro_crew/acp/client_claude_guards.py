"""The ``agent.claude_guards`` setting: the Settings switch for the broader starting-mode pin.

``AcpClient._pin_claude_starting_mode`` (``client_claude_session.py``) moves a claude
session that carries Crew's tools out of an approving start (``auto``,
``bypassPermissions``, or an unreported mode) to ``default`` when the broader pin is on.
Two levers turn it on, and either is enough: the operator environment variable
``KIROCREW_CC_PIN_INHERITED_MODE`` (``resolve_cc_pin_inherited_mode``) and this setting.
The pin itself is unchanged; this module only answers whether the setting is on.

It is a point-of-use read, taken once per session start: the live watcher's snapshot
when the process has one, else a disk load off the event loop. So a change applies to
each chat's next ``session/new`` or ``session/load`` with no restart and no applier
(config.md, "`restart=True` is the single source of restart truth").

The read fails toward the pin. The setting is a guard, so a config that cannot be read,
or whose ``agent`` section the loader discarded, reads as on for that session, with a
warning: a person who turned it on never loses it to a torn file. The cost of that
direction is only that a session asks Crew where it otherwise would not have.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

#: The dotted config path, named once for the warning text and the tests.
CLAUDE_GUARDS_CONFIG_KEY = "agent.claude_guards"


async def _config_now() -> "KiroCrewConfig":
    # Function-scope imports, as ``client_keeper._config_async`` reads its own
    # setting: the config loader stays off this module's import path.
    from kiro_crew.config import live

    snap = live.snapshot()
    if snap is not None:
        return snap
    from kiro_crew.config.loader import KiroCrewConfig

    return await asyncio.to_thread(KiroCrewConfig.load)


async def claude_guards_setting_on() -> bool:
    """Whether ``agent.claude_guards`` is on for the session starting now."""
    try:
        cfg = await _config_now()
    except Exception:
        logger.warning(
            "could not read the config to check %s; this claude session is pinned to "
            "the asking mode as if it were on",
            CLAUDE_GUARDS_CONFIG_KEY,
            exc_info=True,
        )
        return True
    if "agent" in cfg.degraded_sections:
        logger.warning(
            "the config's agent section could not be read, so %s is unknown; this "
            "claude session is pinned to the asking mode as if it were on",
            CLAUDE_GUARDS_CONFIG_KEY,
        )
        return True
    return cfg.agent.claude_guards is True
