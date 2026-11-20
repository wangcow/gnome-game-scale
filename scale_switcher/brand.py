"""Wangcow Corporation identity for Scale Switcher.

The product line has to stay visible in the window and in the About
footer, and it has to be kept in redistributions. See NOTICE.
"""

from __future__ import annotations

from pathlib import Path

APP_ID = "io.github.scaleswitcher.ScaleSwitcher"
DEVELOPER_NAME = "Wangcow Corporation"
PRODUCT_LINE = "Product of the Wangcow Corporation"
COPYRIGHT_LINE = "Copyright 2026 Wangcow Corporation"
WEBSITE = "https://wangcow.com"
ISSUE_URL = "https://github.com/wangcow/gnome-game-scale"

_ICON_FILE = f"{APP_ID}.png"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def icons_root() -> Path:
    """Directory that contains the hicolor theme."""
    return project_root() / "data" / "icons"


def app_icon_path(size: int = 256) -> Path:
    return icons_root() / "hicolor" / f"{size}x{size}" / "apps" / _ICON_FILE
