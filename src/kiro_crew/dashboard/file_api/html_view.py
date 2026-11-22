"""The file viewer's HTML page route: ``GET /api/file-html``.

A ``srcdoc`` frame filled from ``/api/file-read`` cannot show a large page:
that body stops at ``_FILE_READ_CAP`` characters, and a self-contained mock page
carries its images inline as base64, so a 60 MB page would show none of them. A
``srcdoc`` document also has no URL of its own: a ``#section`` link resolves
against the dashboard's URL and navigates the frame away instead of scrolling it.

This route serves the WHOLE page from a real URL, so the frame loads it
the way a browser loads any page and its fragment links scroll in place. It is
the same open-and-check envelope as ``/api/file-raw`` (``_open_checked``:
validation, the sensitive-path 403, no-follow open, bounded read) and the same
credential pass as ``/api/file-read``, with one difference in the pass's
subject: a ``data:image`` payload whose decoded bytes sniff as a raster image is
image bytes, which ``/api/file-raw`` serves without a text scan, and it skips
the redactor here too, all but its last ``_STREAM_HOLDBACK_JWT_MAX`` characters.
Every other byte of the page is redacted. The redactor
reads a 60 MB page of base64 in about a minute; the text around the images is
a few kilobytes.

The response is ``Content-Security-Policy: sandbox`` with no allow flags, the
same posture the viewer's ``sandbox=""`` frame gives it, so the page is an
opaque origin that runs no script even when its URL is opened top-level.
"""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Callable

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _FILE_READ_SNIFF_BYTES,
        _HTML_DATA_IMAGE_RE,
        _HTML_VIEW_EXTS,
        _HTML_VIEW_IN_FLIGHT,
        _HTML_VIEW_MAX_BYTES,
        _HTML_VIEW_RENDER_POOL,
        _HTML_VIEW_TEXT_MAX_BYTES,
        _PAYLOAD_MARKS,
        _STREAM_HOLDBACK_JWT_MAX,
        SNIFF_BYTES,
        _html_page_redactor,
        _html_page_send,
        _open_checked,
        _OpenRefusal,
        _PathProbeBusy,
        _probe_busy_response,
        _run_path_probe,
        _sel,
        base64,
        require_owner_dashboard_request,
        sniff_raster_mime,
    )


def _html_raster_payload_spans(data: bytes) -> list[tuple[int, int]]:
    """Byte spans of the ``data:image`` payloads in *data* that decode to a raster.

    The declared type is ignored: a payload is skipped only when the head of its
    decoded bytes passes ``sniff_raster_mime``, the sniff ``/api/file-raw``
    serves an image file by, and holds a non-text byte, and each span stops ``_STREAM_HOLDBACK_JWT_MAX``
    characters short of the payload's end. A payload that does not decode, or
    decodes to anything else, stays in the redacted text whole.
    """
    spans = []
    for match in _HTML_DATA_IMAGE_RE.finditer(data):
        start, end = match.span(1)
        # Four base64 characters are three bytes; read enough for every sniff.
        head_len = min(end - start, ((SNIFF_BYTES + 2) // 3) * 4) // 4 * 4
        try:
            head = base64.b64decode(data[start : start + head_len], validate=True)
        except ValueError:
            continue
        # The shared sniffer only, not ``/api/file-raw``'s TIFF and ICO rows. A
        # BMP, GIF or WebP signature is printable, so a head must also hold a
        # byte no text holds: every real raster's does (PNG 0x89, JPEG 0xFF,
        # the NULs in a small size or dimension field).
        if sniff_raster_mime(head) is None or not any(b < 0x09 or b >= 0x80 for b in head):
            continue
        # The payload's tail stays in the redacted text: a credential glued to
        # its end runs on in base64's alphabet, so the redactor must see the run
        # from as far back as its own longest token. A comma opens the payload,
        # so nothing can be glued to its start.
        end -= _STREAM_HOLDBACK_JWT_MAX
        if end > start:
            spans.append((start, end))
    return spans


def _redact_html_page(
    data: bytes, redact_fn: Callable[[str], str]
) -> list[bytes | memoryview] | None:
    """*data* with every byte outside a verified raster payload redacted, in order.

    The chunks are the redacted text and views into *data* for the payloads, so
    the reply is written without a second copy of the page. Returns ``None``
    when the text the pass reads is over ``_HTML_VIEW_TEXT_MAX_BYTES``. Blocking
    and CPU-bound; callers run it on a worker thread.

    The text is redacted in ONE pass, payloads replaced by a mark, so a
    credential that straddles two text pieces is still seen whole. If the pass
    does not return every mark it was given, the payloads are left out rather
    than spliced back into the wrong places: the page loses its images, never
    its redaction.
    """
    spans = _html_raster_payload_spans(data)
    pieces: list[bytes] = []
    last = 0
    for start, end in spans:
        pieces.append(data[last:start])
        last = end
    pieces.append(data[last:])
    if sum(len(p) for p in pieces) > _HTML_VIEW_TEXT_MAX_BYTES:
        return None
    # Each cut sits between ASCII bytes (``,`` before a payload, base64 at its
    # end), so decoding the pieces apart splits no character.
    texts = [p.decode("utf-8", errors="replace") for p in pieces]
    mark = next((m for m in _PAYLOAD_MARKS if not any(m in t for t in texts)), None)
    if mark is None:
        # The page holds every mark already: redact it whole, images included,
        # under the same cap on what the pass reads.
        if len(data) > _HTML_VIEW_TEXT_MAX_BYTES:
            return None
        return [redact_fn(data.decode("utf-8", errors="replace")).encode("utf-8")]
    redacted = redact_fn(mark.join(texts)).split(mark)
    if len(redacted) != len(texts):
        return ["".join(redacted).encode("utf-8")]
    out: list[bytes | memoryview] = []
    view = memoryview(data)
    for i, text in enumerate(redacted):
        out.append(text.encode("utf-8"))
        if i < len(spans):
            out.append(view[spans[i][0] : spans[i][1]])
    return out


async def api_file_html(request: web.Request) -> web.StreamResponse:
    """GET /api/file-html?path=... — an .html file, whole, for the viewer's sandboxed frame."""
    owner_denied = await require_owner_dashboard_request(request, "file_html")
    if owner_denied is not None:
        return owner_denied
    raw_path = request.query.get("path", "")
    if not raw_path.lower().endswith(_HTML_VIEW_EXTS):
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_html", outcome="denied", resources=raw_path
        )
        return web.json_response({"error": "not an html file", "code": "not_html"}, status=400)
    if not _HTML_VIEW_IN_FLIGHT.acquire(blocking=False):
        return _probe_busy_response(resource=raw_path, tool_name="file_html")
    try:
        return await _serve_html_page(request, raw_path)
    finally:
        _HTML_VIEW_IN_FLIGHT.release()


async def _serve_html_page(request: web.Request, raw_path: str) -> web.StreamResponse:
    """:func:`api_file_html` past its gates: read, check, redact, write."""
    try:
        opened = await _run_path_probe(
            functools.partial(
                _open_checked, raw_path, tool_name="file_html", max_bytes=_HTML_VIEW_MAX_BYTES
            ),
            transfer=True,
            stall_path=raw_path,
        )
    except _PathProbeBusy:
        return _probe_busy_response(resource=raw_path, tool_name="file_html")
    if isinstance(opened, _OpenRefusal):
        return opened.response
    path = opened.path
    # The name checked above is the caller's; the file opened must be html too.
    if not str(path).lower().endswith(_HTML_VIEW_EXTS):
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_html", outcome="denied", resources=path
        )
        return web.json_response({"error": "not an html file", "code": "not_html"}, status=400)
    # /api/file-read's binary verdict: a NUL in the sniff window. A UTF-16 page
    # interleaves NULs with its text, so no credential pattern matches it, and a
    # browser would still show it as text.
    if b"\x00" in opened.data[:_FILE_READ_SNIFF_BYTES]:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_html",
            outcome="denied",
            resources=path,
            error="binary",
        )
        return web.json_response({"error": "not a text file", "code": "binary"}, status=415)
    redact_fn = await _html_page_redactor(request)
    chunks = await asyncio.get_running_loop().run_in_executor(
        _HTML_VIEW_RENDER_POOL, _redact_html_page, opened.data, redact_fn
    )
    if chunks is None:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_html",
            outcome="denied",
            resources=path,
            error="file_too_large",
        )
        return web.json_response({"error": "file too large", "code": "file_too_large"}, status=413)
    _sel().log_tool_invocation(
        session_key="dashboard", tool_name="file_html", outcome="success", resources=path
    )
    return await _html_page_send(request, chunks)
