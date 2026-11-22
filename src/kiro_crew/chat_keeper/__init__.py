"""A chat's adapter process that outlives the gateway: the chat keeper.

A keeper is a small process that owns one chat adapter's stdio. The gateway starts it
in the adapter's place; it starts the adapter as its own child, relays the adapter's
stdio over an authenticated unix socket, and journals every stdout line while no
gateway is attached. The next gateway attaches to it, replays what it missed, and the
chat carries on in the same adapter process.

Design and invariants: ``docs/system-specs/modules/runtime-ownership.md``, section
"Chats that outlive the gateway: the chat keeper".

This package's ``__init__`` stays import-light: the keeper process imports it, and
the keeper must start fast and load nothing a gateway-only module needs.
"""

from __future__ import annotations

from pathlib import Path

#: The data-home leaf holding every keeper's record, key and socket. A HIDDEN
#: sandbox leaf (``sandbox_leaves._CREW_HIDDEN_LEAVES``): nothing sandboxed reads
#: or writes it.
KEEPERS_DIR_NAME = "chat-keepers"

#: A keeper's socket is ``<instance><SOCKET_SUFFIX>`` in the keepers directory. Short
#: on purpose, like the 12-hex instance id: AF_UNIX caps the whole path at 103 bytes
#: and a data home can sit deep.
SOCKET_SUFFIX = ".s"

#: Hex digits in a keeper instance id (48 bits: unique within one data home).
INSTANCE_HEX_DIGITS = 12

#: The module the keeper process runs.
KEEPER_MODULE = "kiro_crew.chat_keeper.keeper"

#: The argv token every keeper carries, so a process-table reader can tell a keeper
#: from any other Python process without parsing the rest of its command line.
KEEPER_ARGV_MARKER = "--kirocrew-chat-keeper"

#: How long a keeper with no gateway attached waits for one before it ends its
#: adapter. A fixed constant, not a setting: long enough for an update's restart.
ADOPT_SECS = 300

#: How long a freshly started keeper waits for its first attachment. The gateway
#: that started it attaches within milliseconds; a keeper whose gateway died in
#: between ends itself after this.
FIRST_ATTACH_SECS = 60

#: SIGTERM-to-SIGKILL grace when a keeper ends its own process group.
KEEPER_TERM_GRACE_SECS = 5.0


def supported() -> bool:
    """Whether this platform can run a chat keeper (POSIX only; see the spec)."""
    from kiro_crew import platform_compat

    return bool(platform_compat.IS_POSIX)


def keepers_dir(home: Path | None = None) -> Path:
    """``<data home>/chat-keepers``; *home* defaults to the gateway's data home."""
    if home is None:
        from kiro_crew.config.paths import data_home

        home = data_home()
    return Path(home) / KEEPERS_DIR_NAME
