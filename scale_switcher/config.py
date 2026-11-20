"""JSON settings and the small runtime state file.

Config (user-edited, also written by the GUI):
    ~/.config/scale-switcher/config.json

Runtime state (written when the scale actually changes):
    ~/.config/scale-switcher/state.json

The state file records whether we currently believe a game session is
active, and the scale each monitor had before it was set to 100%. If the
process dies mid-game, the next start uses it to put those scales back
once no monitored app is left.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("scale_switcher")

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "scale-switcher"
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

# How often to scan /proc, and how long a start or stop must stay true
# before the scale changes. All are clamped when the file is loaded.
#
# The start delay is short so a game creates its window at 100%. The stop
# delay is longer: a launcher handing off to the game, a game restarting
# itself after a patch, or Wine shutting down should not bounce the
# desktop back to its usual scale for a moment.
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_START_DELAY_SECONDS = 0.5
DEFAULT_STOP_DELAY_SECONDS = 4.0
MAX_DELAY_SECONDS = 60.0
# Replaced by the two delays above. Dropped from the file on load.
_LEGACY_KEYS = ("debounce_seconds",)


def _clamp(value: object, default: float, low: float, high: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _normalize_process_name(raw: str) -> str:
    """Store a process or executable basename, not a full path."""
    text = raw.strip()
    if not text or "\n" in text or "\x00" in text:
        return ""
    # Accept a pasted path (`/usr/bin/steam`, `C:\Games\Wow.exe`) and keep
    # the file name.
    name = text.replace("\\", "/").rstrip("/").split("/")[-1].strip()
    if not name or name in {".", ".."}:
        return ""
    if len(name) > 255:
        return ""
    return name


class Config:
    """User settings. Mutate fields, then call save()."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CONFIG_PATH
        self.enabled = True
        self.notifications = True
        self.processes: list[str] = []
        self.poll_interval_seconds = DEFAULT_POLL_SECONDS
        self.start_delay_seconds = DEFAULT_START_DELAY_SECONDS
        self.stop_delay_seconds = DEFAULT_STOP_DELAY_SECONDS
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            log.info("No config at %s; using defaults", self.path)
            self.save()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._quarantine(exc)
            self.save()
            return
        if not isinstance(data, dict):
            self._quarantine(ValueError("config root is not an object"))
            self.save()
            return

        self.enabled = bool(data.get("enabled", True))
        self.notifications = bool(data.get("notifications", True))
        names: list[str] = []
        seen: set[str] = set()
        raw_processes = data.get("processes", [])
        if isinstance(raw_processes, list):
            for item in raw_processes:
                if not isinstance(item, str):
                    continue
                name = _normalize_process_name(item)
                key = name.casefold()
                if name and key not in seen:
                    seen.add(key)
                    names.append(name)
        self.processes = names
        cleaned = raw_processes != names
        self.poll_interval_seconds = _clamp(
            data.get("poll_interval_seconds", DEFAULT_POLL_SECONDS),
            DEFAULT_POLL_SECONDS,
            0.5,
            10.0,
        )
        self.start_delay_seconds = _clamp(
            data.get("start_delay_seconds", DEFAULT_START_DELAY_SECONDS),
            DEFAULT_START_DELAY_SECONDS,
            0.0,
            MAX_DELAY_SECONDS,
        )
        self.stop_delay_seconds = _clamp(
            data.get("stop_delay_seconds", DEFAULT_STOP_DELAY_SECONDS),
            DEFAULT_STOP_DELAY_SECONDS,
            0.0,
            MAX_DELAY_SECONDS,
        )
        log.info(
            "Loaded config from %s (%d process name(s), enabled=%s, notifications=%s)",
            self.path,
            len(self.processes),
            self.enabled,
            self.notifications,
        )
        if cleaned:
            # Older versions stored full Windows paths from the picker.
            log.info("Normalized the process list to file names: %s", ", ".join(names) or "(empty)")
        legacy = [key for key in _LEGACY_KEYS if key in data]
        if legacy:
            log.info(
                "Replaced %s with start_delay_seconds (%.1fs) and stop_delay_seconds (%.1fs)",
                ", ".join(legacy),
                self.start_delay_seconds,
                self.stop_delay_seconds,
            )
        if cleaned or legacy:
            self.save()

    def save(self) -> None:
        payload = {
            "enabled": self.enabled,
            "notifications": self.notifications,
            "processes": list(self.processes),
            "poll_interval_seconds": self.poll_interval_seconds,
            "start_delay_seconds": self.start_delay_seconds,
            "stop_delay_seconds": self.stop_delay_seconds,
        }
        try:
            _atomic_write(self.path, json.dumps(payload, indent=2) + "\n")
        except OSError as exc:
            log.error("Could not write config %s: %s", self.path, exc)

    def add_process(self, raw: str) -> str | None:
        """Add a process name. Return the stored name, or None if rejected."""
        name = _normalize_process_name(raw)
        if not name:
            return None
        if any(existing.casefold() == name.casefold() for existing in self.processes):
            return None
        self.processes.append(name)
        self.save()
        log.info("Now monitoring process name %r", name)
        return name

    def remove_process(self, name: str) -> None:
        key = name.casefold()
        kept = [item for item in self.processes if item.casefold() != key]
        if len(kept) == len(self.processes):
            return
        self.processes = kept
        self.save()
        log.info("Stopped monitoring process name %r", name)

    def _quarantine(self, exc: BaseException) -> None:
        backup = self.path.with_name(self.path.name + ".bad")
        log.error("Config %s is unreadable (%s). Moving it to %s", self.path, exc, backup)
        try:
            self.path.replace(backup)
        except OSError as move_exc:
            log.error("Could not move broken config aside: %s", move_exc)
        self.enabled = True
        self.notifications = True
        self.processes = []
        self.poll_interval_seconds = DEFAULT_POLL_SECONDS
        self.start_delay_seconds = DEFAULT_START_DELAY_SECONDS
        self.stop_delay_seconds = DEFAULT_STOP_DELAY_SECONDS


class RuntimeState:
    """Game-mode latch and the per-monitor scales to restore afterwards."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE_PATH
        self.gaming_mode = False
        # Monitor key (see scale.LogicalMonitor.key) -> desktop scale.
        self.restore_scales: dict[str, float] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Ignoring unreadable state file %s: %s", self.path, exc)
            return
        if not isinstance(data, dict):
            return
        self.gaming_mode = bool(data.get("gaming_mode", False))
        scales = data.get("restore_scales")
        if isinstance(scales, dict):
            for key, value in scales.items():
                if isinstance(key, str) and isinstance(value, (int, float)) and 0.5 <= value <= 4:
                    self.restore_scales[key] = float(value)
        log.info(
            "Runtime state: gaming_mode=%s, %d saved monitor scale(s)",
            self.gaming_mode,
            len(self.restore_scales),
        )

    def set_gaming_mode(self, gaming: bool) -> None:
        self.gaming_mode = gaming
        self._save()

    def set_restore_scales(self, scales: dict[str, float]) -> None:
        self.restore_scales = dict(scales)
        self._save()

    def _save(self) -> None:
        payload = {"gaming_mode": self.gaming_mode, "restore_scales": self.restore_scales}
        try:
            _atomic_write(self.path, json.dumps(payload, indent=2) + "\n")
        except OSError as exc:
            log.error("Could not write state %s: %s", self.path, exc)
