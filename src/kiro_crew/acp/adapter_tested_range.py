"""The adapter releases Crew's contracts were recorded on, and the notice for one outside them.

Moved whole from :mod:`kiro_crew.acp.client_turn_control`, which re-exports every name
here, so ``from kiro_crew.acp.client_turn_control import untested_version_issue`` and
the ``*_TESTED_*`` constants still import from where they always did.
"""

from __future__ import annotations

from kiro_crew.acp.client_claude_session import _adapter_version_at_least
from kiro_crew.acp.types import ACP_BACKEND_GOOSE, ACP_BACKEND_KIRO, ACP_BACKEND_PI
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_LAUNCH,
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
)

#: The goose release Crew's goose contract was recorded on: the goose capture README
#: (``test/fixtures/acp_frames/goose/README.md``) names 1.50.1 for every capture but
#: ``effort-live.jsonl`` (1.54.0, effort only), ``handshake-live.jsonl`` reports it,
#: and ``agent_sdk/backends.py`` declares the VERIFIED RANGE 1.50.x (the range
#: ``GOOSE_VERIFIED_VERSION_PREFIX`` logs against). Steer and effort were also measured
#: on 1.54.0, but the routing contract was not. Lower bound inclusive, upper
#: exclusive. goose is not in the real-adapter lane, so nothing measures another line.
GOOSE_TESTED_VERSION = "1.50.1"
GOOSE_TESTED_RANGE = ((1, 50, 0), (1, 51, 0))

#: The codex-acp release the Real Adapter Contract lane runs: ``test/real_adapters/
#: package.json`` pins it, and every live codex capture's ``agentInfo.version`` reports
#: it. codex-acp numbers its releases by semver, so the tested range is its major.
CODEX_ACP_TESTED_VERSION = "2.3.0"
CODEX_ACP_TESTED_RANGE = ((2, 0, 0), (3, 0, 0))

#: The claude-agent-acp release the Real Adapter Contract lane pins in
#: ``test/real_adapters/package.json``, which the live claude captures report. Its
#: releases are 0.x, where each minor line can break, so the tested range is 0.94.x.
CLAUDE_ACP_TESTED_VERSION = "0.94.0"
CLAUDE_ACP_TESTED_RANGE = ((0, 94, 0), (0, 95, 0))

#: The pi-acp release the same lane pins (spawning pi 1.4.0), which the live pi captures
#: report. It is 0.0.x, where each patch can break, so the range is that release.
PI_ACP_TESTED_VERSION = "0.0.34"
PI_ACP_TESTED_RANGE = ((0, 0, 34), (0, 0, 35))

#: The kiro-cli releases Crew's kiro-cli and KAS relay contract was recorded on. No
#: lane runs a live kiro-cli (it needs a sign-in), so the range is held to what the
#: captures under ``test/fixtures/acp_frames/kiro`` and ``kas`` were taken on:
#: ``test/test_real_adapter_fixture_pins.py`` holds every live capture's kiro-cli
#: inside it, and the Docker pin (``packaging/kiro-cli-version``) too. The floor is
#: the captures' floor, the highest of the gates that test names; the top is the major after
#: the newest capture (3.1.0), as codex-acp's range is its major. A capture on 4.x
#: reds that test until this moves with it.
KIRO_CLI_TESTED_RANGE = ((2, 27, 0), (4, 0, 0))

#: Per backend: the tested range and the command that installs the tested release.
#: Opt-in by membership, as ``client_turn_control._STEER_MIN_VERSIONS``.
_TESTED_VERSION_RANGES: dict[str, tuple[tuple[tuple[int, int, int], tuple[int, int, int]], str]] = {
    ACP_BACKEND_GOOSE: (
        GOOSE_TESTED_RANGE,
        ACP_BACKEND_LAUNCH[ACP_BACKEND_GOOSE].install_command.replace(
            "| bash", f"| GOOSE_VERSION={GOOSE_TESTED_VERSION} bash"
        ),
    ),
    ACP_BACKEND_CODEX: (
        CODEX_ACP_TESTED_RANGE,
        f"npm i -g {ACP_BACKEND_NODE_ADAPTER_PACKAGES[ACP_BACKEND_CODEX]}"
        f"@{CODEX_ACP_TESTED_RANGE[0][0]}",
    ),
    ACP_BACKEND_CLAUDE: (
        CLAUDE_ACP_TESTED_RANGE,
        f"npm i -g {ACP_BACKEND_NODE_ADAPTER_PACKAGES[ACP_BACKEND_CLAUDE]}"
        f"@{CLAUDE_ACP_TESTED_VERSION}",
    ),
    ACP_BACKEND_PI: (
        PI_ACP_TESTED_RANGE,
        f"npm i -g {ACP_BACKEND_NODE_ADAPTER_PACKAGES[ACP_BACKEND_PI]}@{PI_ACP_TESTED_VERSION}",
    ),
    # kiro-cli installs no chosen release, so an older one is told to update.
    ACP_BACKEND_KIRO: (KIRO_CLI_TESTED_RANGE, "kiro-cli update"),
}

#: What a release above the range is told instead of the install command, for a
#: harness whose installer cannot go back to a tested release.
_NEWER_THAN_TESTED_ADVICE: dict[str, str] = {
    ACP_BACKEND_KIRO: "A Kiro Crew update moves the tested range.",
}

#: The name a notice gives a backend whose id is not its name.
_NOTICE_NAMES: dict[str, str] = {ACP_BACKEND_KIRO: "kiro-cli"}


def _tested_range_label(low: tuple[int, int, int], high: tuple[int, int, int]) -> str:
    """``2.x`` for a whole major, ``0.0.34`` for one release, ``2.27.0 to 3.x`` from a
    release up to a major's end, else ``1.50.x`` for a minor line."""
    if low[1:] == (0, 0) and high == (low[0] + 1, 0, 0):
        return f"{low[0]}.x"
    if high == (low[0], low[1], low[2] + 1):
        return ".".join(str(part) for part in low)
    if high[1:] == (0, 0) and high[0] > low[0]:
        return f"{'.'.join(str(part) for part in low)} to {high[0] - 1}.x"
    return f"{low[0]}.{low[1]}.x"


def untested_version_issue(backend: str, agent_version: str) -> str:
    """The notice for an *agent_version* outside *backend*'s tested range, else ``""``.

    A warning only: the session still runs. A backend with no tested range, and an
    absent or unreadable version, gets none. Parsed by :func:`_adapter_version_at_least`,
    the reader the goose steer floor uses, so the two notices read one version alike.
    """
    range_and_fix = _TESTED_VERSION_RANGES.get(backend)
    if range_and_fix is None or not _adapter_version_at_least(agent_version, (0, 0, 0)):
        return ""
    (low, high), fix = range_and_fix
    newer = _adapter_version_at_least(agent_version, high)
    if _adapter_version_at_least(agent_version, low) and not newer:
        return ""
    tested = _tested_range_label(low, high)
    advice = (newer and _NEWER_THAN_TESTED_ADVICE.get(backend)) or (
        f"To install the tested release, run '{fix}', then start a new chat."
    )
    return (
        f"{_NOTICE_NAMES.get(backend, backend)} {agent_version} is outside the tested "
        f"range {tested}, so parts of it may behave in ways Crew has not checked. {advice}"
    )
