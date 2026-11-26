"""Session controls: the compact controls an app adds to the composer bar, and their keys.

``SessionControlContribution`` is one control, ``SessionControlKey`` one key that opens
its box, and the caps and the key grammar here are mirrored by the dashboard
(``website/src/hooks/useSessionControls.ts``, ``website/src/lib/appSessionKeys.ts``), so
a change to one is a change to both sides.

Served through :mod:`kiro_crew.apps.manifest` and
:mod:`kiro_crew.apps.manifest_contributes`, which callers and tests import; this module
imports neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kiro_crew.apps.manifest_route_grammar import normalize_status_path as _normalize_status_path

#: Hard cap on session controls per app. A control here renders inside the
#: composer, on the path of every turn — an app must not be able to crowd the
#: bar out. Apps needing more configuration have a page for it.
MAX_SESSION_CONTROLS_PER_APP = 2
#: A session control's popover width, in CSS px: the default, and the widest an
#: app may ask for with ``width``. Mirrored by ``SESSION_CONTROL_WIDTH`` in
#: ``website/src/hooks/useSessionControls.ts``. The cap is the composer's box at
#: the narrowest desktop window (768 px wide, the phone breakpoint), measured on a
#: test Crew in the default layout: x 350 to 746, so a popover anchored on the
#: chip at the box's left edge stays over the composer.
SESSION_CONTROL_DEFAULT_WIDTH = 340
SESSION_CONTROL_MAX_WIDTH = 396
#: Keys one session control may declare (``keys``), and the most bytes a key's
#: ``arg`` may take as JSON. Mirrored by ``MAX_SESSION_CONTROL_KEYS`` and
#: ``parseKeyDefault`` in ``website/src/lib/appSessionKeys.ts``.
MAX_SESSION_CONTROL_KEYS = 4
SESSION_CONTROL_KEY_ARG_MAX_BYTES = 4096
_KEY_MODIFIERS = ("mod", "ctrl", "alt", "shift")
_KEY_NAMED = frozenset({"Enter", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"})
_KEY_PUNCT = frozenset(",./\\[]`-=;'")


def session_control_key_default_error(raw: str) -> str:
    """Why ``raw`` is not a key default, or ``''`` when it is one.

    The grammar: ``Mod``, ``Ctrl``, ``Alt`` and ``Shift`` (any case, each at most
    once) joined by ``+``, then one key: a letter, a digit, US punctuation
    (``, . / \\ [ ] ` - = ; '``), ``Enter`` or an arrow. ``Mod`` is Cmd on a Mac and
    Ctrl elsewhere. It must hold Mod, Ctrl or Alt: a bare key, or Shift and a key,
    would fire while the person types.
    """
    parts = raw.split("+")
    key = parts[-1]
    mods = [p.lower() for p in parts[:-1]]
    if any(m not in _KEY_MODIFIERS for m in mods) or len(set(mods)) != len(mods):
        return "modifiers must be Mod, Ctrl, Alt or Shift, each once, joined by +"
    if not (
        (len(key) == 1 and (key.isascii() and key.isalnum() or key in _KEY_PUNCT))
        or key in _KEY_NAMED
    ):
        return "the key must be a letter, a digit, US punctuation, Enter or an arrow"
    if not {"mod", "ctrl", "alt"} & set(mods):
        return "it must hold Mod, Ctrl or Alt"
    return ""


@dataclass
class SessionControlKey:
    """A key a person can press to open the control's box in the chat on screen.

    The person sees one row per key in Settings → Shortcuts → Apps and may
    change it there; ``default`` is only the starting key, and a default one of
    Crew's own keys (or an earlier app's) holds is turned off rather than taken.
    The box reads ``arg`` through ``useSessionControlRequest()`` as
    ``{via: 'key', payload: arg}``.
    """

    id: str = ""  # slug, unique within the control
    label: str = ""  # names the Settings row, after the app's name
    # Optional, e.g. "Alt+Shift+Q"; anything but a string is kept as is for
    # ``validate`` to refuse.
    default: Any = ""
    arg: Any = None  # optional JSON handed to the box; None = none

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "label": self.label}
        if self.default:
            d["default"] = self.default
        if self.arg is not None:
            d["arg"] = self.arg
        return d

    @classmethod
    def from_dict(cls, data: Any) -> SessionControlKey:
        if not isinstance(data, dict):
            return cls()
        return cls(
            id=str(data.get("id", "")),
            label=data.get("label", "") if isinstance(data.get("label", ""), str) else "",
            default=data.get("default", ""),
            arg=data.get("arg"),
        )


@dataclass
class SessionControlContribution:
    """A compact control an app contributes to the session (composer) bar.

    The slot exists because per-session app configuration has nowhere else to
    live: without it an app contributes a sidebar page and nothing else, so a
    setting scoped to "this chat" has to be set on a separate page against an
    opaque session key the app cannot even discover.

    Rendered by the dashboard as a lazily-imported ESM module, exactly like a
    page, so the existing import map (react, the app SDK) and the single-React
    guarantee apply unchanged. The component is handed the active session's
    identity as props — that is the whole point of the slot.

    ``statusPath`` is optional and lets the chip carry state *before* it is
    opened. Without it a control can only report anything once its module is
    lazily imported, which is on first click — so a per-session setting looks
    unset until you go looking for it. The dashboard GETs
    ``<the app's own route base>/<statusPath>?session_key=…`` and reads
    ``{state, tooltip}``, where state is ``ok`` | ``warn`` | ``none``.

    That base depends on how the app serves its backend, because the two are
    mounted at different prefixes: an app with in-gateway hook routes answers
    under ``/api/apps/<app>/``, while an app running its own backend PROCESS is
    reverse-proxied at ``/apps/<app>/api/``. The dashboard picks the prefix from
    the manifest rather than trusting a declared one, so a control does not have
    to know which it is.
    """

    id: str = ""  # stable per-app identifier, e.g. "env-picker"
    entryPoint: str = ""  # ESM bundle path relative to ui/  # noqa: N815
    label: str = ""  # accessible name; also the tooltip
    icon: str = ""  # lucide icon name
    statusPath: str = ""  # optional backend route reporting per-session chip state  # noqa: N815
    # Optional popover width on a desktop window, in CSS px; 0 = the default.
    # Anything but an int in [default, max] is refused at install (-1 here).
    width: int = 0
    # Keys that open this control's box (see SessionControlKey).
    keys: list[SessionControlKey] = field(default_factory=list)
    #: Whether ``keys`` was present but not a list, for ``validate`` to refuse.
    bad_keys: bool = False
    # Optional: the box opens in the composer dock, between the chat and the
    # message box, instead of as a popover on the chip. Only the JSON ``true``
    # turns it on; anything but a boolean is kept as is for ``validate`` to refuse.
    dock: Any = False
    # Optional, with ``dock`` only: a press outside the docked box leaves it open.
    stayOpen: Any = False  # noqa: N815

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "entryPoint": self.entryPoint}
        if self.width:
            d["width"] = self.width
        if self.dock is True:
            d["dock"] = True
        if self.stayOpen is True:
            d["stayOpen"] = True
        if self.keys:
            d["keys"] = [k.to_dict() for k in self.keys]
        if self.label:
            d["label"] = self.label
        if self.icon:
            d["icon"] = self.icon
        if self.statusPath:
            d["statusPath"] = self.statusPath
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionControlContribution:
        raw_keys = data.get("keys", [])
        return cls(
            id=str(data.get("id", "")),
            entryPoint=str(data.get("entryPoint", "")),  # noqa: N815
            label=str(data.get("label", "")),
            icon=str(data.get("icon", "")),
            statusPath=_normalize_status_path(data.get("statusPath", "")),  # noqa: N815
            width=_session_control_width(data["width"]) if "width" in data else 0,
            keys=(
                [SessionControlKey.from_dict(k) for k in raw_keys]
                if isinstance(raw_keys, list)
                else []
            ),
            bad_keys=not isinstance(raw_keys, list),
            dock=data.get("dock", False),
            stayOpen=data.get("stayOpen", False),  # noqa: N815
        )


def session_control_dock_errors(ctl: SessionControlContribution) -> list[str]:
    """Why a control's ``dock`` / ``stayOpen`` would be refused at install, one line each.

    ``stayOpen`` needs ``dock``: a popover that stayed open would float over the
    reply being read, since the transcript's bottom padding measures the dock only.
    """
    name = f"session control contribution {ctl.id or '<unnamed>'}"
    errors = [
        f"{name} {field_name} must be true or false"
        for field_name, value in (("dock", ctl.dock), ("stayOpen", ctl.stayOpen))
        if not isinstance(value, bool)
    ]
    if ctl.stayOpen is True and ctl.dock is not True:
        errors.append(f"{name} stayOpen needs dock: true")
    return errors


def _session_control_width(raw: Any) -> int:
    """A declared ``width`` as an int, or -1 for anything ``validate`` must refuse:
    not a plain int (a bool, ``null``), or ``0``, which would read as "absent"."""
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw != 0 else -1
