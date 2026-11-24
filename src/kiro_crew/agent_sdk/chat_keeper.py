"""Chat Keeper's ACP half, through the agent-backend boundary.

The keeper clients live in :mod:`kiro_crew.acp.client_keeper`. The dashboard's
restart adopt reaches them here, so the boundary gate sees one surface. Each call
looks the ACP name up when it runs, so importing this module costs nothing and a
test that patches ``client_keeper`` is seen through it.
"""

from __future__ import annotations

from typing import Any


def keeps(agent: object) -> bool:
    """Whether the *agent* config section runs its claude chats on keepers."""
    from kiro_crew.acp import client_keeper

    return client_keeper._keeps(agent)


def holder_of(instance: str) -> Any:
    """The client attached to keeper *instance*, or None when none is."""
    from kiro_crew.acp import client_keeper

    return client_keeper.holder_of(instance)


def keeper_process(client: Any) -> Any:
    """*client*'s keeper process, or None when it runs on none."""
    from kiro_crew.acp import client_keeper

    return client_keeper.keeper_process(client)


async def end_unadopted(record: Any, why: str) -> None:
    """End the keeper *record* names, which no chat adopted, saying *why*."""
    from kiro_crew.acp import client_keeper

    await client_keeper._end_unadopted(record, why)


__all__ = ["end_unadopted", "holder_of", "keeper_process", "keeps"]
