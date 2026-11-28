"""Whether a link-preview card may put its chat into the answering mode, as a grant.

A provider that declares ``answering: true`` (with ``sessionKey: true``) may call
its pinned card's ``card.answering.enter`` on the person's click or key in the
card: Crew then draws an answer line under the card and holds the keys the mode
needs, and the person's own Shift+Enter, received by Crew's page, sends the text
the card last handed over to that chat as their message. The card never sends:
its whole new authority is to hand text over for Crew to show and to start or end
the mode. That is still a widening (the text can become the person's message on
their press), so it is its own grant family, ``linkPreviewAnswering``, with the
one entry ``answering``, stored per app beside the other link-preview families.

An install approves what it declares; an update that adds ``answering`` holds it
back until the owner approves it on the app's page. The family is registered
explicit-only (``point_grants``): an older host ignored the field, so a stored set
written there never showed it to the owner and approves nothing in it. Nothing
here names an app.
"""

from __future__ import annotations

from typing import Any

#: The grant family this module derives.
LINK_PREVIEW_ANSWERING_FAMILY = "linkPreviewAnswering"
LINK_PREVIEW_ANSWERING_GRANT_FAMILIES = (LINK_PREVIEW_ANSWERING_FAMILY,)
#: The one entry of the family. Mirrored by ``ANSWERING_GRANT`` in
#: ``website/src/pages/appDetailPageStagedGrants.ts``.
ANSWERING_ENTRY = "answering"


def _declares_answering(contributes: Any) -> bool:
    """Whether the first link-preview provider declares ``answering`` with ``sessionKey``."""
    # Deferred: link_preview_grants is imported by point_grants as well.
    from kiro_crew.apps.link_preview_grants import _first_provider  # noqa: PLC0415

    provider = _first_provider(contributes)
    return bool(
        provider is not None
        and provider.get("answering") is True
        and provider.get("sessionKey") is True
    )


def link_preview_answering_grant_entries(contributes: Any) -> dict[str, list[str]]:
    """The grant family a manifest's ``contributes`` declares."""
    return {
        LINK_PREVIEW_ANSWERING_FAMILY: [ANSWERING_ENTRY] if _declares_answering(contributes) else []
    }


def answering_grant_fields(name: str, contributes: Any) -> dict[str, bool]:
    """``answering`` (approved) and ``stagedAnswering`` (declared, waiting) for an app row."""
    if not _declares_answering(contributes):
        return {"answering": False, "stagedAnswering": False}
    # Deferred: the facade's family imports ``point_grants``, which imports this module.
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415

    granted = ANSWERING_ENTRY in staged_app_grants(
        name, LINK_PREVIEW_ANSWERING_FAMILY, lambda: [ANSWERING_ENTRY]
    )
    return {"answering": granted, "stagedAnswering": not granted}


__all__ = [
    "ANSWERING_ENTRY",
    "LINK_PREVIEW_ANSWERING_FAMILY",
    "LINK_PREVIEW_ANSWERING_GRANT_FAMILIES",
    "answering_grant_fields",
    "link_preview_answering_grant_entries",
]
