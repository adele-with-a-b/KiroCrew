"""The ``crew_client`` feature preview: edition hooks for a Crew client lane.

Behind ``dashboard.feature_previews.crew_client``, default off. On, Crew honours
two hooks an edition fills at boot:

- a sign-in profile for one IAM Identity Center start URL
  (``auth.login.endpoints.register_idc_profile``): its own client registration
  form, and a pinned profile ARN so its sign-in asks no control plane. A stored
  sign-in on such a start URL also fences the usage refresh and local KAS starts;
- an engine-credential provider for Crew's own KAS engine bundle
  (``acp.kas_engine_auth.register_engine_auth``).

The dashboard also lets a finished Remote Crew launch card close. Off, every
registered hook is ignored and Crew behaves as on ``main``. Nothing here holds a
start URL, an ARN, a host or a token: an edition supplies them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

logger = logging.getLogger(__name__)

#: The preview's name under ``dashboard.feature_previews``.
CREW_CLIENT_PREVIEW: Final[str] = "crew_client"


def _config(cfg: Any) -> Any:
    """*cfg*, else the live snapshot, else the config file; None when unreadable."""
    if cfg is not None:
        return cfg
    try:
        from kiro_crew.config import live

        snap = live.snapshot()
        if snap is not None:
            return snap
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load()
    except Exception:  # noqa: BLE001 -- unread means off
        logger.debug("crew_client preview: config unreadable; treating as off", exc_info=True)
        return None


def preview_on(cfg: Any = None) -> bool:
    """Whether the edition permits an exact ``True`` stored ``crew_client`` switch.

    Read through ``edition_locks.feature_previews`` like every server reader of a
    preview switch, so an LTS build reads it as off. Never raises.
    """
    from kiro_crew.edition_locks import feature_previews

    previews = feature_previews(
        getattr(getattr(_config(cfg), "dashboard", None), "feature_previews", None)
    )
    return isinstance(previews, Mapping) and previews.get(CREW_CLIENT_PREVIEW) is True
