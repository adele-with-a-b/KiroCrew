"""``contributes.statusItems``: short live readouts an app shows in the top bar.

A status item is a glanceable value the operator wants in view on every page -- a
countdown, a health word -- not a control. The manifest declares only what is fixed
(an id, the accessible label, an icon name from the app icon set, and the route that
reports the value); the dashboard GETs that route about once a minute and draws the
answer ``{text, tone, tooltip}`` as a pill on the desktop top bar and in the phone
chat header. Nothing app-specific is in core: which app, which label and which route
all come from the manifest.

Bounded hard because the surface is shared: the top bar is one row on every page, so
an app gets at most :data:`MAX_STATUS_ITEMS_PER_APP`, and the dashboard caps the bar as
a whole (``MAX_STATUS_ITEMS`` in ``website/src/hooks/useAppStatusItems.ts``).

Parsed into :class:`~kiro_crew.apps.manifest_contributes.Contributes` and validated by
its ``validate``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kiro_crew.apps.manifest_route_grammar import (
    APP_ITEM_ID_RE,
    APP_STATUS_PATH_RE,
    normalize_status_path,
)

#: Most status items one app may contribute. Two, because the top bar is shared by
#: every installed app and a readout pair (a value and its deadline) is the largest
#: honest unit; anything wider belongs on the app's own page.
MAX_STATUS_ITEMS_PER_APP = 2

#: Longest accepted label. It is the pill's accessible name and the row heading in the
#: pill's detail card, never drawn on the pill itself, so it stays a short noun phrase.
MAX_STATUS_ITEM_LABEL = 40

#: Longest accepted icon name. The dashboard resolves it against a fixed set and draws
#: a generic glyph for anything else, so this only bounds the stored string.
_MAX_ICON_NAME = 40


@dataclass
class StatusItemContribution:
    """One top-bar readout an app contributes.

    ``statusPath`` is a route inside the app's own backend, under the prefix the
    dashboard derives from how the app serves it (``/apps/<app>/api/`` for a
    backend process, ``/api/apps/<app>/`` for in-gateway hook routes), exactly as
    for a session control's status route.
    """

    id: str = ""
    label: str = ""
    icon: str = ""
    statusPath: str = ""  # noqa: N815

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "label": self.label, "statusPath": self.statusPath}
        if self.icon:
            d["icon"] = self.icon
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StatusItemContribution:
        return cls(
            id=str(data.get("id", "")),
            label=str(data.get("label", "")),
            icon=str(data.get("icon", "")),
            statusPath=normalize_status_path(data.get("statusPath", "")),  # noqa: N815
        )


def parse_status_items(data: dict[str, Any]) -> tuple[list[StatusItemContribution], bool]:
    """Parse ``contributes.statusItems``; return the items and whether the value was not a list.

    A non-object ENTRY becomes an empty placeholder rather than being dropped, so it
    fails the required-field checks and the install is refused with a reason; a dropped
    entry would install clean and simply never appear.
    """
    raw = data.get("statusItems", [])
    if not isinstance(raw, list):
        return [], "statusItems" in data
    return [
        StatusItemContribution.from_dict(e) if isinstance(e, dict) else StatusItemContribution()
        for e in raw
    ], False


def status_item_errors(items: list[StatusItemContribution], bad: bool) -> list[str]:
    """Install-time refusals for ``contributes.statusItems``."""
    errors: list[str] = []
    if bad:
        errors.append(
            "contributes.statusItems must be an array -- a non-array value passes as "
            "empty and then disappears from the serialized manifest, so the app author "
            "sees neither an error nor any pill"
        )
    if len(items) > MAX_STATUS_ITEMS_PER_APP:
        errors.append(
            f"contributes.statusItems: at most {MAX_STATUS_ITEMS_PER_APP} per app "
            f"(declared {len(items)}) -- the top bar is shared by every app"
        )
    seen: set[str] = set()
    for item in items:
        name = item.id or "<unnamed>"
        if not item.id:
            errors.append("status item contribution missing required field: id")
        elif not APP_ITEM_ID_RE.fullmatch(item.id):
            errors.append(f"status item contribution id must be kebab-case: {item.id!r}")
        elif item.id in seen:
            errors.append(f"status item contribution id is duplicated: {item.id!r}")
        else:
            seen.add(item.id)
        if not item.label.strip():
            errors.append(f"status item contribution {name} missing required field: label")
        elif len(item.label) > MAX_STATUS_ITEM_LABEL:
            errors.append(
                f"status item contribution {name} label is longer than "
                f"{MAX_STATUS_ITEM_LABEL} characters"
            )
        if len(item.icon) > _MAX_ICON_NAME:
            errors.append(f"status item contribution {name} icon name is too long")
        if not item.statusPath:
            # Required: the pill's whole content comes from this route, so an item
            # without one could only ever render empty.
            errors.append(f"status item contribution {name} missing required field: statusPath")
        elif not APP_STATUS_PATH_RE.fullmatch(item.statusPath):
            errors.append(
                f"status item contribution {name} statusPath must be a relative backend "
                f"route (lowercase, no scheme, host or query): {item.statusPath!r}"
            )
    return errors
