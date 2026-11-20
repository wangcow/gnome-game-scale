"""Identify the OS and desktop session before any display change.

The scale call is Mutter's, which is the GNOME display server. Fedora,
Debian, and Arch all use that same call when the running session is
GNOME. KDE Plasma on those distros, and any session that is not GNOME,
must be left alone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DESKTOP_ENV_KEYS = ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")


@dataclass(frozen=True)
class Platform:
    """What was detected, and whether a scale change is allowed."""

    os_id: str
    os_like: tuple[str, ...]
    os_name: str
    desktop: str
    desktop_raw: str
    session: str

    @property
    def can_change_scale(self) -> bool:
        return self.desktop == "gnome"

    def block_reason(self) -> str | None:
        if self.can_change_scale:
            return None
        return (
            f"{_os_label(self)}, {_desktop_label(self)}, {_session_label(self.session)}. "
            "Scale Switcher only changes the display on GNOME, so the scale was left alone."
        )

    def describe(self) -> str:
        if self.can_change_scale:
            return (
                f"{_os_label(self)}, {_desktop_label(self)}, {_session_label(self.session)}. "
                "Display changes use Mutter."
            )
        return self.block_reason() or ""


def current_platform() -> Platform:
    """Read ``/etc/os-release`` and the session environment."""
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        text = ""
    return detect_platform(os.environ, text)


def detect_platform(environ: dict[str, str], os_release: str) -> Platform:
    """Build a platform from already-loaded text. Used by tests."""
    info = _parse_os_release(os_release)
    os_id = info.get("ID", "").strip().lower()
    like = tuple(
        part.strip().lower()
        for part in info.get("ID_LIKE", "").split()
        if part.strip()
    )
    pretty = info.get("PRETTY_NAME", "").strip()
    if not pretty:
        name = info.get("NAME", "").strip()
        version = info.get("VERSION_ID", "").strip()
        pretty = f"{name} {version}".strip()
    tokens = _desktop_tokens(environ)
    session = environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session not in {"wayland", "x11"}:
        session = ""
    return Platform(
        os_id=os_id,
        os_like=like,
        os_name=pretty,
        desktop=_classify_desktop(tokens),
        desktop_raw=";".join(tokens),
        session=session,
    )


def _parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _desktop_tokens(environ: dict[str, str]) -> list[str]:
    found: list[str] = []
    for key in _DESKTOP_ENV_KEYS:
        raw = environ.get(key, "").strip()
        if not raw:
            continue
        # DESKTOP_SESSION is sometimes a path to a session file.
        raw = raw.replace("\\", "/").split("/")[-1]
        for part in raw.replace(":", ";").replace(",", ";").split(";"):
            part = part.strip()
            if part and part not in found:
                found.append(part)
    return found


def _classify_desktop(tokens: list[str]) -> str:
    folded = [token.casefold() for token in tokens]
    gnome = any(token == "gnome" or token.startswith("gnome-") for token in folded)
    kde = any(
        token == "kde" or token == "plasma" or token.startswith("plasma") or token.startswith("kde")
        for token in folded
    )
    # Both names at once is ambiguous. Do not change the display.
    if gnome and kde:
        return "unknown"
    if gnome:
        return "gnome"
    if kde:
        return "kde"
    return "unknown"


def _os_label(platform: Platform) -> str:
    return platform.os_name or "Unknown OS"


def _desktop_label(platform: Platform) -> str:
    if platform.desktop == "gnome":
        return "GNOME"
    if platform.desktop == "kde":
        return "KDE Plasma"
    if platform.desktop_raw:
        return platform.desktop_raw
    return "unknown desktop"


def _session_label(session: str) -> str:
    if session == "wayland":
        return "Wayland"
    if session == "x11":
        return "X11"
    return "unknown session"
