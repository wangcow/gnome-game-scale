#!/usr/bin/env python3
"""Launch Scale Switcher.

Run from anywhere:

    python3 /path/to/gnome-game-scale/scale_switcher.py
    python3 /path/to/gnome-game-scale/scale_switcher.py --background
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python3 scale_switcher.py` without installing the package.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from scale_switcher.app import main  # noqa: E402
except ModuleNotFoundError as exc:
    missing = exc.name or "a required module"
    sys.stderr.write(
        f"Missing Python module {missing!r}.\n"
        "On Fedora, install the system packages:\n"
        "  sudo dnf install python3-gobject python3-psutil gtk4 libadwaita\n"
    )
    raise SystemExit(1)
except ValueError as exc:
    # gi.require_version raises ValueError when GTK 4 or libadwaita is absent.
    sys.stderr.write(
        f"{exc}\n"
        "On Fedora, install the system packages:\n"
        "  sudo dnf install python3-gobject python3-psutil gtk4 libadwaita\n"
    )
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
