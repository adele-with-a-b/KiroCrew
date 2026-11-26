"""What a docked session control may put in the message box, as a normalized grant entry.

A session control that declares ``composerInsert: true`` (with ``dock: true``) may
call the SDK's ``useInsertIntoComposer()`` from its docked box: the host puts the
text in its chat's message box at the caret and answers only whether it did. The
app reads nothing back: not the draft, the selection or a send. Its whole new
authority is that one write, so the grant is the control itself, ``<id>:insert``,
stored per app in the approved-grants file beside the other point families.

An install approves what it declares; an update that adds a control's
``composerInsert`` holds it back until the owner approves it on the app's page,
like a new composer action. The family is registered explicit-only
(``point_grants``): an older host ignored the field, so a stored set written there
never showed it to the owner and approves nothing in it. Nothing here names an app.
"""

from __future__ import annotations

from typing import Any

#: The grant family this module derives.
SESSION_CONTROL_INSERT_FAMILY = "sessionControlInserts"
SESSION_CONTROL_INSERT_GRANT_FAMILIES = (SESSION_CONTROL_INSERT_FAMILY,)
#: The one effect: text into the message box.
INSERT_EFFECT = "insert"


def control_insert_grant(control_id: str) -> str:
    """The grant entry of one control's insert."""
    return f"{control_id}:{INSERT_EFFECT}"


def _inserting_ids(contributes: Any) -> list[str]:
    """Ids of the session controls that declare ``composerInsert: true``, in order."""
    if contributes is None:
        return []
    raw = (
        contributes.get("sessionControls")
        if isinstance(contributes, dict)
        else getattr(contributes, "sessionControls", None)
    )
    ids: list[str] = []
    for entry in raw if isinstance(raw, list) else []:
        declared = (
            entry.get("composerInsert")
            if isinstance(entry, dict)
            else getattr(entry, "composerInsert", None)
        )
        control_id = entry.get("id") if isinstance(entry, dict) else getattr(entry, "id", None)
        if (
            declared is True
            and isinstance(control_id, str)
            and control_id
            and control_id not in ids
        ):
            ids.append(control_id)
    return ids


def session_control_insert_grant_entries(contributes: Any) -> dict[str, list[str]]:
    """The grant family a manifest's ``contributes`` declares."""
    return {
        SESSION_CONTROL_INSERT_FAMILY: [
            control_insert_grant(i) for i in _inserting_ids(contributes)
        ]
    }


def session_control_insert_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``sessionControlInsertGrant`` key of an app row, absent when none is declared.

    ``approved`` are the control ids the dashboard lets insert; ``staged`` the ids
    an update added that wait for the owner.
    """
    contributes = (
        manifest.get("contributes")
        if isinstance(manifest, dict)
        else getattr(manifest, "contributes", None)
    )
    ids = _inserting_ids(contributes)
    if not ids:
        return {}
    declared = [control_insert_grant(i) for i in ids]
    # Deferred: the facade's family imports ``point_grants``, which imports this module.
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415

    granted = set(staged_app_grants(name, SESSION_CONTROL_INSERT_FAMILY, lambda: declared))
    approved = [i for i in ids if control_insert_grant(i) in granted]
    return {
        "sessionControlInsertGrant": {
            "approved": approved,
            "staged": [i for i in ids if i not in approved],
        }
    }


__all__ = [
    "INSERT_EFFECT",
    "SESSION_CONTROL_INSERT_FAMILY",
    "SESSION_CONTROL_INSERT_GRANT_FAMILIES",
    "control_insert_grant",
    "session_control_insert_grant_entries",
    "session_control_insert_grant_row",
]
