"""``GET /api/file-html``: a whole .html page for the file viewer's sandboxed frame.

The rows pin what the route must keep from its siblings -- the sensitive-path 403,
the credential pass over every byte that is not a verified raster payload, an
empty CSP sandbox -- and what it adds: a raster ``data:image`` payload comes back
byte for byte, so a mock page past ``/api/file-read``'s cap shows its images.
"""

from __future__ import annotations

import base64
import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard import server as server_mod
from kiro_crew.dashboard.handlers import api_file_html
from kiro_crew.dashboard.handlers import files as files_mod
from kiro_crew.platform import redact_via_context
from kiro_crew.security import redaction_switch
from kiro_crew.testing.synthetic_credentials import github_classic

#: A PNG's signature and a body long enough that its payload outruns the tail
#: the redactor keeps reading.
_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 64


def _data_uri(raw: bytes, mime: str = "image/png") -> bytes:
    return f"data:{mime};base64,".encode() + base64.b64encode(raw)


def _app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-html", api_file_html)
    return as_owner(app)


@pytest.fixture
def redaction_on(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The owner's switch reads ON; OFF (the default) stands the owner's pass down."""
    _floor_monkeypatch.setattr(redaction_switch, "_path", lambda: tmp_path / "redaction.json")
    _floor_monkeypatch.setattr(redaction_switch, "DEFAULT_REDACTION_ENABLED", True)


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


async def _get(path) -> tuple[int, dict, bytes]:
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get("/api/file-html", params={"path": str(path)})
        return resp.status, dict(resp.headers), await resp.read()


@pytest.mark.asyncio
async def test_raster_payload_is_served_byte_for_byte_and_the_text_is_redacted(
    tmp_path, mock_sel, redaction_on
):
    token = github_classic()
    uri = _data_uri(_PNG)
    page = b'<a href="#s2">go</a><img src="' + uri + b'"><p id="s2">' + token.encode() + b"</p>"
    f = tmp_path / "mock.html"
    f.write_bytes(page)
    status, headers, body = await _get(f)
    assert status == 200
    assert uri in body
    assert token.encode() not in body
    assert b"[REDACTED" in body
    assert headers["Content-Type"].startswith("text/html")


@pytest.mark.asyncio
async def test_a_credential_in_the_page_text_outside_a_data_payload_is_redacted(
    tmp_path, mock_sel, redaction_on
):
    """Before, between and after raster payloads, in an attribute and in a script."""
    tokens = [github_classic(seed=s) for s in (11, 12, 13, 14)]
    uri = _data_uri(_PNG)
    page = (
        b'<meta name="k" content="' + tokens[0].encode() + b'">'
        b'<img src="' + uri + b'">'
        b"<script>const t = '" + tokens[1].encode() + b"'</script>"
        b'<img src="' + uri + b'">'
        b'<a href="https://example.test/?token=' + tokens[2].encode() + b'">x</a>'
        b"<p>" + tokens[3].encode() + b"</p>"
    )
    f = tmp_path / "mock.html"
    f.write_bytes(page)
    status, _headers, body = await _get(f)
    assert status == 200
    for token in tokens:
        assert token.encode() not in body
    assert body.count(uri) == 2


@pytest.mark.asyncio
async def test_a_data_payload_whose_head_is_not_a_raster_is_redacted(
    tmp_path, mock_sel, redaction_on
):
    """Declared ``image/png``, decoding to text that holds a credential: it is redacted.

    The payload is longer than the tail every payload keeps in the pass, so only
    the raster sniff keeps its head in the redactor's text.
    """
    token = github_classic(seed=21)
    text = b"export GITHUB_TOKEN=" + token.encode() + b"\n" + b"# notes\n" * 2000
    payload = base64.b64encode(text)
    assert len(payload) > 2 * files_mod._STREAM_HOLDBACK_JWT_MAX
    page = b'<img src="' + _data_uri(_PNG) + b'"><img src="data:image/png;base64,' + payload + b'">'
    f = tmp_path / "mock.html"
    f.write_bytes(page)
    status, _headers, body = await _get(f)
    assert status == 200
    assert payload[:200] not in body
    assert b"[REDACTED" in body
    assert _data_uri(_PNG) in body


@pytest.mark.asyncio
async def test_the_owner_with_the_switch_off_sees_the_page_as_written(tmp_path, mock_sel):
    """The same per-request verdict /api/file-read renders the owner's view by."""
    token = github_classic()
    f = tmp_path / "mock.html"
    f.write_bytes(b"<p>" + token.encode() + b"</p>")
    status, _, body = await _get(f)
    assert status == 200
    assert token.encode() in body


@pytest.mark.asyncio
async def test_the_response_is_an_empty_sandbox(tmp_path, mock_sel):
    f = tmp_path / "mock.html"
    f.write_bytes(b"<p>hi</p>")
    status, headers, _ = await _get(f)
    assert status == 200
    csp = headers["Content-Security-Policy"]
    directives = [d.strip() for d in csp.split(";")]
    # An empty sandbox directive: no script, no same-origin, nothing allowed.
    assert "sandbox" in directives
    assert "allow-" not in csp
    assert "script-src" not in csp  # default-src 'none' covers it
    assert "default-src 'none'" in directives
    assert headers["X-Content-Type-Options"] == "nosniff"
    # The sources a srcdoc frame inherited from the dashboard's policy, no wider.
    base = [d.strip() for d in server_mod._BASE_CSP.split(";")]
    for name in ("style-src", "img-src", "font-src", "media-src"):
        assert [d for d in directives if d.startswith(name + " ")] == [
            d for d in base if d.startswith(name + " ")
        ]


@pytest.mark.asyncio
async def test_requests_past_the_in_flight_cap_answer_busy(tmp_path, mock_sel, monkeypatch):
    """Each request holds its whole page, so a burst of reloads is refused, not queued."""
    f = tmp_path / "mock.html"
    f.write_bytes(b"<p>hi</p>")
    monkeypatch.setattr(files_mod, "_HTML_VIEW_IN_FLIGHT", threading.BoundedSemaphore(1))
    assert files_mod._HTML_VIEW_IN_FLIGHT.acquire(blocking=False)
    status, _, body = await _get(f)
    assert status == 503 and b"hi" not in body
    files_mod._HTML_VIEW_IN_FLIGHT.release()
    status, _, body = await _get(f)
    assert status == 200 and body == b"<p>hi</p>"


@pytest.mark.asyncio
async def test_the_streamed_page_carries_the_dashboard_security_headers(tmp_path, mock_sel):
    """The head goes out before ``no_cache_middleware`` runs, so the route sets them."""
    f = tmp_path / "mock.html"
    f.write_bytes(b"<p>hi</p>")
    status, headers, body = await _get(f)
    assert status == 200 and body == b"<p>hi</p>"
    assert "no-store" in headers["Cache-Control"]
    assert headers["X-Frame-Options"]


@pytest.mark.asyncio
async def test_a_utf16_page_is_refused_as_binary(tmp_path, mock_sel, redaction_on):
    """File-read's binary verdict: NULs between the characters hide a credential
    from every pattern, and a browser would still show it as text."""
    token = github_classic()
    f = tmp_path / "mock.html"
    f.write_bytes(("<p>" + token + "</p>").encode("utf-16"))
    status, _, body = await _get(f)
    assert status == 415
    assert token.encode("utf-16-le") not in body


@pytest.mark.asyncio
async def test_the_sensitive_path_rung_answers_403(tmp_path, mock_sel):
    """``_open_checked``'s re-check. A real ``~/.ssh/…`` path is refused one rung
    earlier, by path validation, as 400 ``invalid or forbidden path``."""
    f = tmp_path / "mock.html"
    f.write_bytes(b"<p>secret page</p>")
    with patch.object(files_mod, "is_sensitive_path", return_value=True):
        status, _, body = await _get(f)
    assert status == 403
    assert b"secret page" not in body


@pytest.mark.asyncio
async def test_a_file_that_is_not_html_is_refused_before_it_is_read(tmp_path, mock_sel):
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    with patch.object(files_mod, "_open_checked") as opened:
        status, _, _ = await _get(f)
    assert status == 400
    opened.assert_not_called()


@pytest.mark.asyncio
async def test_a_page_over_the_byte_cap_answers_413(tmp_path, mock_sel, monkeypatch):
    monkeypatch.setattr(files_mod, "_HTML_VIEW_MAX_BYTES", 64)
    f = tmp_path / "mock.html"
    f.write_bytes(b"<p>" + b"x" * 100 + b"</p>")
    status, _, _ = await _get(f)
    assert status == 413


@pytest.mark.asyncio
async def test_text_over_the_redaction_cap_answers_413(tmp_path, mock_sel, monkeypatch):
    monkeypatch.setattr(files_mod, "_HTML_VIEW_TEXT_MAX_BYTES", 64)
    f = tmp_path / "mock.html"
    # The image is not counted against the text cap; the text is.
    f.write_bytes(b'<img src="' + _data_uri(_PNG) + b'">' + b"x" * 100)
    status, _, _ = await _get(f)
    assert status == 413


def _redactor_input(page: bytes) -> str:
    seen: list[str] = []

    def spy(text: str) -> str:
        seen.append(text)
        return text

    out = files_mod._redact_html_page(page, spy)
    assert out is not None and b"".join(out) == page
    return "".join(seen)


def test_only_a_payload_that_decodes_to_a_raster_skips_the_redactor():
    raster = base64.b64encode(_PNG)
    # Declared a PNG, but its bytes are not one: the declared type decides nothing.
    fake = base64.b64encode(b"not an image " * 2000)
    # /api/file-raw's four-byte ICO row, which text can start with: not a raster here.
    ico_head = base64.b64encode(b"\x00\x00\x01\x00" + b"not an icon " * 2000)
    page = (
        b'<img src="data:image/png;base64,' + raster + b'">'
        b'<img src="data:image/png;base64,' + fake + b'">'
        b'<img src="data:image/x-icon;base64,' + ico_head + b'">'
    )
    # Text that starts with a printable raster signature passes the sniff alone.
    text_heads = [
        base64.b64encode(head + b" notes\n" * 2000)
        for head in (b"BM", b"GIF89a", b"RIFF1234WEBPVP8 ")
    ]
    # A real GIF's size fields carry NULs, so it is still an image.
    gif = base64.b64encode(b"GIF89a\x01\x00\x01\x00\x80\x00\x00" + bytes(range(256)) * 64)
    for payload in (*text_heads, gif):
        page += b'<img src="data:image/gif;base64,' + payload + b'">'
    text = _redactor_input(page)
    assert fake.decode() in text
    assert ico_head.decode() in text
    for payload in text_heads:
        assert payload.decode() in text
    assert gif.decode() not in text
    assert raster.decode() not in text
    # The raster payload's tail is still read, so nothing glued to it escapes.
    tail = raster[-files_mod._STREAM_HOLDBACK_JWT_MAX :].decode()
    assert tail in text


def test_a_credential_glued_to_a_raster_payload_is_redacted():
    token = github_classic()
    page = b'<img src="' + _data_uri(_PNG) + token.encode() + b'">'
    chunks = files_mod._redact_html_page(page, redact_via_context)
    assert chunks is not None
    out = b"".join(chunks)
    assert token.encode() not in out
    assert out.startswith(b'<img src="' + _data_uri(_PNG)[: -files_mod._STREAM_HOLDBACK_JWT_MAX])


def test_a_pass_that_drops_a_mark_leaves_the_images_out():
    page = b'<img src="' + _data_uri(_PNG) + b'"><p>t</p>'

    def eats_marks(text: str) -> str:
        for mark in files_mod._PAYLOAD_MARKS:
            text = text.replace(mark, "")
        return text

    chunks = files_mod._redact_html_page(page, eats_marks)
    assert chunks is not None
    assert base64.b64encode(_PNG)[:1000] not in b"".join(chunks)


def test_a_page_holding_every_mark_is_redacted_whole():
    marks = "".join(files_mod._PAYLOAD_MARKS).encode()
    page = marks + b'<img src="' + _data_uri(_PNG) + b'">'
    text = _redactor_input(page)
    assert base64.b64encode(_PNG).decode() in text


def test_a_page_holding_every_mark_keeps_the_text_cap(monkeypatch):
    """Redacted whole, the page's images count against the cap on what the pass reads."""
    monkeypatch.setattr(files_mod, "_HTML_VIEW_TEXT_MAX_BYTES", 1024)
    marks = "".join(files_mod._PAYLOAD_MARKS).encode()
    page = marks + b'<img src="' + _data_uri(_PNG) + b'">'
    assert files_mod._redact_html_page(page, lambda text: text) is None
