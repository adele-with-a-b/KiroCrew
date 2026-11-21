"""Untested-version notices the person dismissed, kept on the gateway.

A new session whose harness reports a release outside the one Crew tested it on
gets a notice row (``AcpProvider.tested_version_notice``). The person can dismiss
it for that release: the dismissal is stored here, per data home, so every
device that reaches this gateway agrees, and a new session of that harness at
that release writes no notice. A different installed release is a different
notice, so it shows again until it is dismissed in turn.

One JSON file, ``notice-dismissals/version_notices.json`` in the config dir, maps
a harness name (``claude``, ``codex``, ``goose``, ``pi``, ``kiro-cli``) to the
releases dismissed for it, oldest first. Keeping every dismissed release rather
than only the newest keeps an older transcript's row hidden after the person
dismisses a newer one. The list is capped; past the cap the oldest release goes.

Only the person's own browser may dismiss (:func:`api_version_notice_dismiss`):
the notice is a warning to the person, so an agent or an app must not be able to
hide it. For the same reason the file sits in its own directory, which the agent
file tools and the OS sandbox both fence, so no agent can write it on disk and
have the gateway read it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.atomic_write import atomic_write
from kiro_crew.bounded_read import bounded_read
from kiro_crew.config.loader import config_dir
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.owner_only_files import mkdirs_owner_only

logger = logging.getLogger(__name__)

#: The fenced directory the file lives in (``security.paths`` and the sandbox's
#: hidden leaves both name it). A directory rather than the bare file, because
#: ``atomic_write`` publishes through a sibling temp that a file-only mask would
#: not cover.
DISMISSALS_DIR = "notice-dismissals"
DISMISSALS_FILE = "version_notices.json"

#: ``meta.kind`` of the notice row, with ``meta.harness`` and ``meta.version``.
#: Mirrored by ``UNTESTED_VERSION_NOTICE_KIND`` in
#: ``website/src/pages/chat/UntestedVersionNotice.tsx``.
NOTICE_KIND = "untested_version_notice"

#: The owner-only WebSocket event a dismissal sends, ``{"harness", "version"}``.
#: Every open page re-reads the dismissals and hides that release's rows.
WS_EVENT = "version_notice_dismissed"

#: Releases kept per harness. Far more than a person installs in practice.
MAX_VERSIONS_PER_HARNESS = 20
MAX_VERSION_CHARS = 64
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]*")
#: Every harness at the cap with the longest releases, JSON-quoted, plus room.
MAX_FILE_BYTES = 16 * MAX_VERSIONS_PER_HARNESS * (MAX_VERSION_CHARS + 4) + 1024
_MAX_BODY_BYTES = 1024

_OPERATION = "chat.version_notice_dismiss"
_LOCK = LoopBoundLock()
#: The stored dismissals per file path. Only this gateway writes the file, so the
#: copy read once stays true until :func:`record` changes both.
_CACHE: dict[str, dict[str, list[str]]] = {}


def known_harnesses() -> frozenset[str]:
    """The harness names a notice can carry, one per backend with a tested range."""
    from kiro_crew.acp.client_turn_control import TESTED_VERSION_NOTICE_HARNESSES

    return TESTED_VERSION_NOTICE_HARNESSES


def store_path(home: Path) -> Path:
    """The dismissals file under *home* (the config dir)."""
    return home / DISMISSALS_DIR / DISMISSALS_FILE


def _valid_version(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_VERSION_CHARS
        and _VERSION_RE.fullmatch(value) is not None
    )


def _read(path: Path) -> bytes | None:
    """The file's bytes; ``None`` when it is missing or is not one this module wrote.

    Never follows a link and never reads past :data:`MAX_FILE_BYTES`, so a link to
    a device or an oversized file planted at the path cannot exhaust memory.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        logger.warning("%s is not a regular file; ignoring it", path.name)
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        data = bounded_read(handle, MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        logger.warning("%s is larger than %d bytes; ignoring it", path.name, MAX_FILE_BYTES)
        return None
    return data


def _parse(data: bytes | None, path: Path) -> dict[str, list[str]]:
    if data is None:
        return {}
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        logger.warning("%s has malformed content: %s", path.name, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    known = known_harnesses()
    out: dict[str, list[str]] = {}
    for harness, versions in raw.items():
        if harness not in known or not isinstance(versions, list):
            continue
        kept = list(dict.fromkeys(v for v in versions if _valid_version(v)))
        if kept:
            out[harness] = kept[-MAX_VERSIONS_PER_HARNESS:]
    return out


def load(home: Path | None = None) -> dict[str, list[str]]:
    """The dismissed releases per harness, oldest first. A copy; ``{}`` when none.

    A file that cannot be read or parsed reads as no dismissals, so the notice
    shows: losing a dismissal costs one more notice, never a hidden warning that
    was not dismissed.
    """
    path = store_path(home if home is not None else config_dir())
    try:
        return _load_for_update(path)
    except OSError:
        logger.warning("could not read %s", path.name, exc_info=True)
        return {}


def _load_for_update(path: Path) -> dict[str, list[str]]:
    """The dismissals at *path*, as the base of a rewrite. A copy.

    Only a missing file reads as none; any other failure to read it raises
    ``OSError``, so :func:`record` abandons its write rather than publish one
    dismissal over the ones a transient read error hid.
    """
    key = str(path)
    if key not in _CACHE:
        _CACHE[key] = _parse(_read(path), path)
    return {harness: list(versions) for harness, versions in _CACHE[key].items()}


def is_dismissed(harness: str, version: str, home: Path | None = None) -> bool:
    """Whether the person dismissed *harness*'s notice at *version*."""
    return version in load(home).get(harness, ())


def record(harness: str, version: str, home: Path | None = None) -> dict[str, list[str]]:
    """Store a dismissal of *harness* at *version*; return the dismissals after it.

    One atomic, owner-only write of the whole file; the cached copy changes only
    once the write succeeded. ``OSError`` propagates, from the write or from a
    read of the stored file that failed for any reason but its absence.
    """
    path = store_path(home if home is not None else config_dir())
    current = _load_for_update(path)
    versions = [v for v in current.get(harness, []) if v != version] + [version]
    current[harness] = versions[-MAX_VERSIONS_PER_HARNESS:]
    mkdirs_owner_only(path.parent)
    atomic_write(path, json.dumps(current, sort_keys=True), fsync=True, restrict_to_owner=True)
    _CACHE[str(path)] = {h: list(v) for h, v in current.items()}
    return current


def notice_meta(client: Any) -> dict[str, str] | None:
    """The row meta that makes *client*'s untested-version notice dismissible, or None.

    None when the session names no harness and release for its notice
    (``tested_version_notice_subject``), as on a KAS relay, whose notice keeps
    showing.
    """
    subject = getattr(client, "tested_version_notice_subject", None)
    if not (isinstance(subject, tuple) and len(subject) == 2):
        return None
    harness, version = subject
    if harness not in known_harnesses() or not _valid_version(version):
        return None
    return {"kind": NOTICE_KIND, "harness": harness, "version": version}


def _clear_cache_for_tests() -> None:
    _CACHE.clear()


def _from_the_persons_browser(request: web.Request) -> bool:
    """Whether *request* is the person's own browser, the only caller that may dismiss.

    The rule ``chat_manual_order`` uses: agents and crew members reach the
    gateway through the MCP loopback helpers, which always send
    ``X-Internal-Secret``, and an app's dashboard token carries an app claim.
    """
    from kiro_crew.dashboard.token_auth import folder_principal, request_origin

    source, _caller = request_origin(request, what="version notice dismissal", log=logger)
    return source == "dashboard" and not folder_principal(request.app["state"], request)


def _refusal(request: web.Request) -> web.Response:
    from kiro_crew.dashboard.token_auth import request_origin
    from kiro_crew.sel import sel

    source, caller = request_origin(request, what="version notice dismissal", log=logger)
    sel().log_api_access(
        caller=caller,
        operation=_OPERATION,
        outcome="denied",
        source=source,
        error="only the person's own browser may dismiss a version notice",
    )
    return web.json_response(
        {
            "error": "only the person's own browser may dismiss a version notice",
            "code": "version_notice_person_only",
        },
        status=403,
    )


async def api_version_notices(request: web.Request) -> web.Response:
    """GET /api/chat/version-notices -- ``{"dismissed": {harness: [release, ...]}}``."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    if (denied := await require_owner_dashboard_request(request, _OPERATION)) is not None:
        return denied
    return web.json_response({"dismissed": await asyncio.to_thread(load)})


async def api_version_notice_dismiss(request: web.Request) -> web.Response:
    """POST /api/chat/version-notices/dismiss -- dismiss one harness's notice at one release.

    Body ``{"harness": "claude", "version": "0.109.0"}``. Answers the dismissals
    after the write and tells every open page (``WS_EVENT``). Refused (403) for any
    caller but the person's own browser; 400 for a harness with no tested range or
    a release that is not a version string.
    """
    from kiro_crew.dashboard.handlers._shared import (
        read_bounded_json,
        require_owner_dashboard_request,
    )

    if (denied := await require_owner_dashboard_request(request, _OPERATION)) is not None:
        return denied
    if not _from_the_persons_browser(request):
        return _refusal(request)
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_BODY_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    harness, version = body.get("harness"), body.get("version")
    if not isinstance(harness, str) or harness not in known_harnesses():
        return web.json_response(
            {"error": "harness names no harness with a tested range", "code": "harness_invalid"},
            status=400,
        )
    if not _valid_version(version):
        return web.json_response(
            {"error": "version must be a release string", "code": "version_invalid"},
            status=400,
        )
    assert isinstance(version, str)
    async with _LOCK:
        try:
            dismissed = await asyncio.to_thread(record, harness, version)
        except OSError:
            logger.warning("could not store a version notice dismissal", exc_info=True)
            return web.json_response(
                {"error": "the dismissal could not be saved", "code": "dismissal_not_saved"},
                status=500,
            )
    request.app["state"].broadcast_ws_owners(WS_EVENT, {"harness": harness, "version": version})
    return web.json_response({"dismissed": dismissed})
