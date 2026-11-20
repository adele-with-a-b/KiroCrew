"""Edition hook: how Crew's own KAS engine bundle gets its credential.

By default Crew is the engine's auth owner: the bundle argv carries
``KAS_BUNDLE_AUTH_ARG`` and Crew answers the engine's credential callback
(``host_auth=True``). An edition that runs Crew on a host which hands the engine a
credential of its own registers a provider here (:func:`register_engine_auth`).
While the ``crew_client`` feature preview is on and the provider answers an
:class:`EngineAuth`, a bundle spawn:

- runs :meth:`EngineAuth.preflight` before any install, so a refused spawn starts
  no engine;
- replaces ``KAS_BUNDLE_AUTH_ARG`` with :meth:`EngineAuth.engine_args`;
- answers no credential callback (``host_auth=False``) and strips Crew's own API
  key from the engine's environment, then lets :meth:`EngineAuth.scrub_env` drop
  more;
- is refused on the kiro-cli relay route, which has no such mode;
- reports a refused start with :meth:`EngineAuth.start_refusal` instead of a
  sign-in prompt.

Off, or with no provider, or when the provider answers None, nothing here changes
a spawn: Crew behaves as on ``main``. Crew holds no flag spelling, host or token
of any provider; the edition supplies all of them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, MutableMapping
from typing import Protocol

logger = logging.getLogger(__name__)


class EngineAuthRefused(Exception):
    """A spawn the provider refuses. The message is shown to the user as written."""


class EngineAuth(Protocol):
    """One spawn's engine credential, as an edition's provider answers it."""

    @property
    def relay_refusal(self) -> str:
        """Why the kiro-cli relay route cannot run under this credential."""

    def preflight(self) -> None:
        """Raise :class:`EngineAuthRefused` when the spawn cannot start. Blocking."""

    def engine_args(self) -> list[str]:
        """The engine's auth arguments, in place of ``KAS_BUNDLE_AUTH_ARG``."""

    def scrub_env(self, env: MutableMapping[str, str]) -> None:
        """Drop from the engine's *env* what it must not see."""

    def start_refusal(self, text: str, code: object) -> str:
        """The user's text for a start the engine refused, or ``""`` when not ours."""


_provider: Callable[[], EngineAuth | None] | None = None
#: The credential the last KAS spawn on this process ran under, set by the harness at
#: each spawn and read on the loop, where config is never read.
_last_spawn: EngineAuth | None = None


def register_engine_auth(provider: Callable[[], EngineAuth | None] | None) -> None:
    """Install (or, with None, remove) the edition's provider. Called at boot."""
    global _provider
    _provider = provider


def active_engine_auth() -> EngineAuth | None:
    """The provider's answer while the ``crew_client`` preview is on, else None.

    Blocking (the provider and the preview may read config); call off the loop. A
    provider that raises is logged and read as None: the default route, Crew's own
    sign-in, is what every install already runs.
    """
    from kiro_crew.crew_client_preview import preview_on

    if not preview_on():
        return None
    # An edition's provider first; else Crew's own machine mode
    # (``agent_sdk/kas_machine.py``), which answers None unless ``agent.kas_auth`` is
    # ``machine``.
    from kiro_crew.agent_sdk.kas_machine import builtin_engine_auth

    provider: Callable[[], EngineAuth | None] = _provider or builtin_engine_auth
    try:
        return provider()
    except Exception:  # noqa: BLE001 -- an edition bug keeps the default route
        logger.warning("KAS engine credential provider failed; using Crew's sign-in", exc_info=True)
        return None


def with_engine_args(argv: list[str], auth: EngineAuth) -> list[str]:
    """*argv* with ``KAS_BUNDLE_AUTH_ARG`` replaced by the provider's arguments."""
    from kiro_crew.acp.kas_transport import KAS_BUNDLE_AUTH_ARG

    if KAS_BUNDLE_AUTH_ARG not in argv:
        raise ValueError("the bundle argv carries no auth argument to replace")
    at = argv.index(KAS_BUNDLE_AUTH_ARG)
    return [*argv[:at], *auth.engine_args(), *argv[at + 1 :]]


def note_spawn(auth: EngineAuth | None) -> None:
    """Record the credential the spawn now starting runs under."""
    global _last_spawn
    _last_spawn = auth


def start_refusal(backend: str, text: str, code: object) -> str:
    """The provider's text for a refused KAS start; ``""`` on every other start."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    auth = _last_spawn
    if backend == ACP_BACKEND_KAS and auth is not None:
        try:
            return auth.start_refusal(text, code) or ""
        except Exception:  # noqa: BLE001 -- the generic sign-in text still answers
            logger.warning("KAS engine credential provider could not word a refusal", exc_info=True)
    return ""
