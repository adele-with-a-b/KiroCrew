"""Kiro auth endpoints and wire constants (mirrors kiro-cli auth/consts.rs).

Both hosts are overridable so an enterprise mirror or a test double can be pointed at:
the portal via ``KIRO_AUTH_PORTAL_URL``, the social service via ``KIRO_AUTH_SERVICE_URL``
(kiro-cli reads the latter from a DB setting; we take it from the environment).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Default hosts (kiro-cli consts.rs).
_DEFAULT_PORTAL = "https://app.kiro.dev"
_DEFAULT_SOCIAL_SERVICE = "https://prod.us-east-1.auth.desktop.kiro.dev"

# The only client identifier the social path sends. Not a secret; it is the
# User-Agent kiro-cli uses, and the portal accepts any caller presenting it.
USER_AGENT = "Kiro-CLI"

# Cognito-allowlisted loopback callback ports (kiro-cli oauth_callback.rs CALLBACK_PORTS).
# The auth service only accepts these pre-registered redirect URIs — do not change
# without auth-service coordination.
CALLBACK_PORTS: tuple[int, ...] = (
    3128,
    4649,
    6588,
    8008,
    9091,
    49153,
    50153,
    51153,
    52153,
    53153,
)

# AWS SSO-OIDC (Builder ID / IAM Identity Center).
BUILDER_ID_REGION = "us-east-1"
BUILDER_ID_START_URL = "https://view.awsapps.com/start"
AMZN_START_URL = "https://amzn.awsapps.com/start"
OIDC_SCOPES = (
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
)
# A scope prefix (``tunnel.relay_viewer_oidc_scope_prefix``, like kiro-cli's
# ``api.oidc.scopePrefix``): lower-case letters and underscores, so it can never add
# a scope or reach past the part before the colon.
OIDC_SCOPE_PREFIX_RE = re.compile(r"[a-z][a-z_]{0,62}")


def oidc_scopes(prefix: str = "") -> tuple[str, ...]:
    """OIDC_SCOPES, or the same scopes under *prefix* when it is a valid one.

    Empty or invalid answers OIDC_SCOPES, so a bad value can only keep the default.
    """
    if not prefix or OIDC_SCOPE_PREFIX_RE.fullmatch(prefix) is None:
        return OIDC_SCOPES
    return tuple(f"{prefix}:{s.partition(':')[2]}" for s in OIDC_SCOPES)


# KasToken.extra key recording a registration's scopes when they are not OIDC_SCOPES,
# so a later sign-in reuses that registration only for the same scopes.
OIDC_SCOPES_EXTRA_KEY = "oidc_scopes"
# client_name we register our own public OAuth client under (kiro-cli uses "Kiro CLI").
OIDC_CLIENT_NAME = "Kiro Crew"

# Device-flow fallback when the poll response omits an expiry (kiro-cli uses 3600).
DEFAULT_TOKEN_TTL_SECS = 3600


@dataclass(frozen=True)
class IdcProfile:
    """One Identity Center start URL's own client registration, filled by an edition."""

    scopes: tuple[str, ...]
    grant_types: tuple[str, ...]
    issuer_url: str
    profile_arn: str = ""


# Empty in Crew itself: every start URL registers with OIDC_SCOPES, as before. An
# edition fills it at boot; it is read only while the ``crew_client`` feature preview
# is on (``kiro_crew.crew_client_preview``), so with the preview off a registered
# profile changes nothing.
_IDC_PROFILES: dict[str, IdcProfile] = {}


def register_idc_profile(
    start_url: str, scopes, grants, issuer_url: str, profile_arn: str = ""
) -> None:
    """Register *start_url*'s scopes, grant types, issuer URL and pinned profile ARN."""
    profile = IdcProfile(tuple(scopes), tuple(grants), issuer_url, profile_arn)
    _IDC_PROFILES[start_url.rstrip("/")] = profile


def idc_profile(start_url: str) -> IdcProfile | None:
    """The profile registered for *start_url*, or ``None`` (the default form).

    ``None`` too while the ``crew_client`` preview is off. Nothing registered reads no
    config at all.
    """
    profile = _IDC_PROFILES.get((start_url or "").rstrip("/"))
    if profile is None:
        return None
    from kiro_crew.crew_client_preview import preview_on

    return profile if preview_on() else None


def registered_start_url(url: str | None) -> bool:
    """Whether *url* is a start URL registered with :func:`register_idc_profile`."""
    return idc_profile(url or "") is not None


def stored_sign_in_registered() -> bool:
    """Whether Crew's stored sign-in is on a registered start URL. Blocking; never raises."""
    from kiro_crew.auth.bridge import vault_identity_claims

    if not _IDC_PROFILES:
        return False
    from kiro_crew.crew_client_preview import preview_on

    # The preview first: while it is off the vault is not read.
    return preview_on() and registered_start_url(vault_identity_claims()[1].get("start_url"))


def portal_url() -> str:
    return os.environ.get("KIRO_AUTH_PORTAL_URL", _DEFAULT_PORTAL).rstrip("/")


def social_service_url() -> str:
    return os.environ.get("KIRO_AUTH_SERVICE_URL", _DEFAULT_SOCIAL_SERVICE).rstrip("/")


def oidc_url(region: str) -> str:
    return f"https://oidc.{region}.amazonaws.com"
