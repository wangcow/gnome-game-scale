"""JSON settings and the small runtime state file.

Config (user-edited, also written by the GUI):
    ~/.config/scale-switcher/config.json

Runtime state (written when the scale actually changes):
    ~/.config/scale-switcher/state.json

The state file records whether we currently believe a game session is
active. If the process dies mid-game, the next start uses it to put the
scale back to 125% once no monitored app is left.
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

# How often to scan /proc, and how long a start/stop must stay true
# before the scale changes. Both are clamped when the file is loaded.
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_DEBOUNCE_SECONDS = 2.0


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
    # Accept a pasted path (`/usr/bin/steam`) and keep the file name.
    name = text.rstrip("/").split("/")[-1].strip()
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
        self.debounce_seconds = DEFAULT_DEBOUNCE_SECONDS
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
        self.poll_interval_seconds = _clamp(
            data.get("poll_interval_seconds", DEFAULT_POLL_SECONDS),
            DEFAULT_POLL_SECONDS,
            0.5,
            10.0,
        )
        self.debounce_seconds = _clamp(
            data.get("debounce_seconds", DEFAULT_DEBOUNCE_SECONDS),
            DEFAULT_DEBOUNCE_SECONDS,
            0.0,
            60.0,
        )
        log.info(
            "Loaded config from %s (%d process name(s), enabled=%s, notifications=%s)",
            self.path,
            len(self.processes),
            self.enabled,
            self.notifications,
        )

    def save(self) -> None:
        payload = {
            "enabled": self.enabled,
            "notifications": self.notifications,
            "processes": list(self.processes),
            "poll_interval_seconds": self.poll_interval_seconds,
            "debounce_seconds": self.debounce_seconds,
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
        self.debounce_seconds = DEFAULT_DEBOUNCE_SECONDS


class RuntimeState:
    """Whether the last successful scale change was into game mode."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE_PATH
        self.gaming_mode = False
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.gaming_mode = False
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Ignoring unreadable state file %s: %s", self.path, exc)
            self.gaming_mode = False
            return
        if isinstance(data, dict):
            self.gaming_mode = bool(data.get("gaming_mode", False))
        else:
            self.gaming_mode = False
        log.info("Runtime state: gaming_mode=%s", self.gaming_mode)

    def set_gaming_mode(self, gaming: bool) -> None:
        self.gaming_mode = gaming
        try:
            _atomic_write(
                self.path,
                json.dumps({"gaming_mode": gaming}, indent=2) + "\n",
            )
        except OSError as exc:
            log.error("Could not write state %s: %s", self.path, exc)
