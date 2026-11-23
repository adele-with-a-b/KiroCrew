"""Open files: an app's dashboard UI opens a local file the way the chat does.

An app that shows a file path (a mock page a question names, a report it wrote) may
let the person open it with one click. The opening itself is the chat's own: the
dashboard's ``POST /api/reveal`` with ``action: "open"``, which refuses a sensitive
path (``security.is_sensitive_path``), a remote browser and anything that is not a
regular file, and hands the rest to the OS default app. Nothing here opens a file.

What this module owns is the permission. An app names it in its manifest as
``permissions.openFiles: true``. It is an entry of the :data:`GRANT_FAMILY` family in
the app's approved-grants set: an install approves what it declares, an update that
adds it holds it back until the owner approves it on the app's page, like a new API
path. The family is registered explicit-only (``point_grants``), so a stored set
written before this host knew the key approves nothing in it.

The gateway publishes the answer on the app row as ``openFilesGrant``. The
dashboard's SDK host (``website/src/components/appFileOpen.ts``) serves
``useOpenFile`` only to an app whose row says approved, and only within
``CLICK_GRANT_MS`` of a real click in that app's own surface. Like every
``useAppApi`` check, that is a guardrail in the dashboard document, not a boundary:
the path rules that protect the person's files are ``/api/reveal``'s own. Nothing
here names an app.
"""

from __future__ import annotations

from typing import Any

#: The manifest key under ``permissions``, and the grant family that stores it.
GRANT_FAMILY = "openFiles"
#: The family's one entry.
GRANT_ENTRY = "open"
OPEN_FILES_GRANT_FAMILIES = (GRANT_FAMILY,)


def declares_open_files(permissions: Any) -> bool:
    """Whether a manifest's ``permissions`` (parsed or a raw dict) declare the grant."""
    if isinstance(permissions, dict):
        return permissions.get(GRANT_FAMILY) is True
    return getattr(permissions, GRANT_FAMILY, False) is True


def open_files_grant_entries(permissions: Any) -> dict[str, list[str]]:
    """The :data:`GRANT_FAMILY` entries a manifest's ``permissions`` declare."""
    return {GRANT_FAMILY: [GRANT_ENTRY] if declares_open_files(permissions) else []}


def _approved(app: str) -> bool:
    """Whether the entry is in *app*'s approved-grants set, read fresh."""
    # Deferred: the facade's family imports ``point_grants``, which imports this module.
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415

    return GRANT_ENTRY in staged_app_grants(app, GRANT_FAMILY, lambda: [GRANT_ENTRY])


def open_files_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``openFilesGrant`` key of an app row, absent when it is undeclared.

    ``approved`` is whether the owner's approved set holds it, which is all the
    dashboard serves; ``staged`` the opposite, which the detail page offers for
    approval.
    """
    permissions = (
        manifest.get("permissions")
        if isinstance(manifest, dict)
        else getattr(manifest, "permissions", None)
    )
    if not declares_open_files(permissions):
        return {}
    approved = _approved(name)
    return {"openFilesGrant": {"approved": approved, "staged": not approved}}


__all__ = [
    "GRANT_ENTRY",
    "GRANT_FAMILY",
    "OPEN_FILES_GRANT_FAMILIES",
    "declares_open_files",
    "open_files_grant_entries",
    "open_files_grant_row",
]
