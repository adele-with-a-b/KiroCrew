"""The grammar shared by contributions that name an app-backend route and a kebab id.

A session control's ``statusPath`` and a status item's ``statusPath`` are both a
route INSIDE the app's own backend that the dashboard GETs on the app's behalf, and
both contributions carry a kebab-case ``id`` the dashboard keys a React list on. One
spelling of each rule, so the two contributions cannot drift into accepting different
paths. Mirrored on the dashboard by ``website/src/lib/sessionControlStatusPath.ts``.

Served through :mod:`kiro_crew.apps.manifest_contributes` (under its session-control
private names) and :mod:`kiro_crew.apps.manifest_status_items`.
"""

from __future__ import annotations

import re
from typing import Any

#: Contribution ids are kebab-case like app names: they appear in a React key, in
#: per-contribution persistence, and potentially in a URL, so the charset is bounded.
APP_ITEM_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: A status route is a path *within* the app's own backend, so it is deliberately
#: not a URL: no scheme, no host, no query. The dashboard appends any query itself,
#: which stops an app from pointing the poll at another origin.
APP_STATUS_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,63}$")


def normalize_status_path(raw: Any) -> str:
    """Normalize a declared ``statusPath`` without laundering a cross-origin URL.

    A single leading slash is a harmless way to write a relative route, so it is
    stripped. A ``//`` prefix is not: that is a protocol-relative URL naming
    another host, and stripping its slashes would turn ``//evilhost/x`` into
    ``evilhost/x`` — which satisfies the route allowlist and reads like an
    ordinary relative path. Such a value is returned unchanged so that
    validation refuses it and the install fails with the real reason, rather
    than silently accepting a rewritten one.

    A host containing a dot happened to be refused anyway, because ``.`` is
    outside the allowlist. A dotless one was not, which is why the guard has to
    be explicit rather than incidental.
    """
    text = str(raw)
    if text.startswith("//"):
        return text
    return text.lstrip("/")
