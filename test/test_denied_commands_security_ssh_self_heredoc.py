"""Tests for how the ssh-to-self floor reads a here-document body.

A body that no program on the line can run as a shell command line is data, so
the floor does not read it as argv: a Python edit script's ``n = a * localhost``
is not a glob verb naming ``ssh`` and its target. A body that a shell, a wrapper,
an unknown program or a piped command could run stays argv, and so does every
body on a line the shared reader may misread (arithmetic, compound syntax, a
continuation, an unusual tag, a second heredoc, the tag seen again).
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_RULE = "sandbox-escape-ssh-self"


@pytest.fixture(autouse=True)
def _pin_own_host_cache(request, _floor_monkeypatch):
    """The own-host pin of ``TestSandboxEscapeSshSelf``: a fixed name cache with the
    resolve-once latch set, so no resolver thread starts, and a DNS alias verdict
    of "not self", so no test resolves a real name."""
    own_mp = pytest.MonkeyPatch()
    request.addfinalizer(own_mp.undo)
    own = security.socket.gethostname().strip().lower()
    pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
    _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    own_mp.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


@pytest.mark.parametrize(
    "cmd",
    [
        # A Python edit script on stdin: a bare ``*`` (multiplication) before a
        # self-host word read as a glob verb naming ``ssh`` and its target.
        "python3 - <<'EOF'\nn = a * localhost\nEOF",
        # The same body written to a file, which the next command runs with Python.
        "cat > s.py <<'EOF'\nn = a * localhost\nEOF\npython3 s.py",
        "python3 -u - <<'PY'\nn = a * localhost\nPY",
        "tee f <<'EOF' >/dev/null\nn = a * localhost\nEOF",
        "cd /w && python3 - <<'EOF'\nn = a * localhost\nEOF",
    ],
)
def test_body_no_program_can_run_is_data(cmd):
    assert _denied_by(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        # A shell reads the body as its script.
        "bash <<'EOF'\nssh localhost id\nEOF",
        "sudo sh <<'EOF'\nssh localhost id\nEOF",
        "cat <<'EOF' | bash\nssh localhost id\nEOF",
        "source /dev/stdin <<'EOF'\nssh localhost id\nEOF",
        # The body is stdin to code that can run it.
        "python3 -c 'import os,sys; os.system(sys.stdin.read())' <<'EOF'\nssh localhost\nEOF",
        "perl -e 'system(<STDIN>)' <<'EOF'\nssh localhost\nEOF",
        "python3 runner.py <<'EOF'\nssh localhost\nEOF",
        # The body is written to a file that a shell then runs.
        "cat > x.sh <<'EOF'\nssh localhost id\nEOF\nbash x.sh",
        "cat > x.sh <<'EOF'\nssh localhost id\nEOF\n./x.sh",
        # An unquoted body runs its substitutions, even for ``cat``.
        "cat <<EOF\n$(ssh localhost id)\nEOF",
        "cat <<EOF\n`ssh localhost id`\nEOF",
        # A real ssh command outside the body.
        "python3 - <<'EOF'\nprint(1)\nEOF\nssh localhost id",
    ],
)
def test_body_a_program_can_run_stays_argv(cmd):
    assert _denied_by(cmd) == _RULE


@pytest.mark.parametrize(
    "cmd",
    [
        # The heredoc command is piped.
        "cat <<'EOF' | cat\nssh -p 22 localhost id\nEOF",
        # Two heredocs on one line.
        "cat <<'A'\nx\nA\ncat <<'B'\nssh -p 22 localhost id\nB",
        # The tag appears again in another letter case: bash keeps the body open.
        "cat <<'EOF'\nEof\nssh -p 22 localhost id\nEOF",
        # An ANSI-C quoted tag.
        "cat <<$'EOF'\ninert\nEOF\nssh -p 22 localhost id",
        # Arithmetic: bash reads ``<<`` as a left shift and runs the ssh line.
        "((cat<<z))\nssh -p 22 localhost id\nz",
        # A continuation can change the delimiter, so any backslash-newline keeps
        # the bodies, one inside a quoted body too.
        "cat <<EO\\\nF\ninert\nEOF\nssh -p 22 localhost id",
        "cat <<'EOF'\ninert\\\ntext\nssh -p 22 localhost id\nEOF",
        # A wrapper, an assignment prefix, or Python running code it was given.
        "env python3 - <<EOF\nssh -p 22 localhost id\nEOF",
        "A=1 python3 - <<EOF\nssh -p 22 localhost id\nEOF",
        "python3 -c x <<EOF\nssh -p 22 localhost id\nEOF",
        "python3 -m x <<EOF\nssh -p 22 localhost id\nEOF",
        "python3 -c x - <<'EOF'\nssh -p 22 localhost id\nEOF",
        # Python reads stdin as its program only when the line names ``-``.
        "python3 <<'EOF'\nssh -p 22 localhost id\nEOF",
        # A brace outside the bodies.
        "cat ${f} <<'EOF'\nssh -p 22 localhost id\nEOF",
        # A ``<<`` inside quotes still counts: the line holds two.
        "cat '<<' <<'EOF'\nssh -p 22 localhost id\nEOF",
        # A tag that is not ASCII, or holds a character other than a word character.
        "cat <<'é'\nssh -p 22 localhost id\né",
        "cat <<'E-F'\nssh -p 22 localhost id\nE-F",
        # The tag appears again inside a body line, in another case.
        "cat <<'EOF'\nssh -p 22 localhost id Eof\nEOF",
        # An unquoted body holds a ``$``.
        "cat <<EOF\n$x\nssh -p 22 localhost id\nEOF",
    ],
)
def test_line_the_reader_may_misread_keeps_bodies_as_argv(cmd):
    assert _denied_by(cmd) == _RULE
