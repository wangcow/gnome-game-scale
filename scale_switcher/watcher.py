"""Find monitored processes, with extra rules for Lutris.

Matching
--------
A configured name matches a process when it equals, case-insensitively, any of:

* ``/proc/<pid>/comm`` (Linux truncates this to 15 bytes)
* the executable basename
* ``argv[0]``'s basename
* any ``*.exe`` token on the command line (Wine puts the game there)

The name ``lutris`` is special. The Lutris client stays open in the
background, so it must not itself flip the scale. While a Lutris client
is running, a game counts when any of these is true:

* A descendant process is a game binary, a Wine/umu/gamescope runtime,
  or ``lutris-wrapper`` (Lutris keeps that wrapper alive for the session).
* Some process has ``LUTRIS_GAME_UUID`` in its environment. Lutris sets
  that on the game, which still works if the game is reparented away
  from the client.
* A game-like executable *appeared after* this Lutris client was first
  seen. Processes that were already running are not treated as the game.
  This covers runners that double-fork out of the Lutris process tree.

Wine's own long-lived services (``wineserver``, ``explorer.exe``,
``services.exe``, …) are ignored for these automatic signals. They often
outlive the game. Adding one of those names to the list explicitly still
matches it through the normal name rule.

Debounce
--------
The scale changes only after the "any game / no game" answer has been
stable for ``debounce_seconds``. One game closing while another is still
open does not restore 125%.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import psutil

from scale_switcher.config import Config, RuntimeState
from scale_switcher.scale import (
    GAME_SCALE,
    NORMAL_SCALE,
    DisplayScaler,
    format_percent,
    scales_close,
)

log = logging.getLogger("scale_switcher")

LUTRIS_NAME = "lutris"

# The Lutris UI and the Flatpak/portal processes around it.
# Matched against every identifier of the process (comm, exe, argv0).
_CLIENT_HELPERS = {
    "lutris",
    "gio",
    "gjs",
    "bwrap",
    "xdg-dbus-proxy",
    "fusermount",
    "fusermount3",
    "xdg-document-portal",
    "xdg-desktop-portal",
    "xdg-desktop-portal-gnome",
}

# Wine processes that keep a prefix alive after the game itself has exited.
# Ignored only for Lutris auto-detection, not for explicit list entries.
_WINE_SERVICES = {
    "explorer.exe",
    "services.exe",
    "plugplay.exe",
    "svchost.exe",
    "rpcss.exe",
    "winedevice.exe",
    "wineboot.exe",
    "winedbg.exe",
    "conhost.exe",
    "start.exe",
    "cmd.exe",
    "rundll32.exe",
    "regsvr32.exe",
    "msiexec.exe",
    "control.exe",
    "winemenubuilder.exe",
    "winecfg.exe",
    "winefile.exe",
    "notepad.exe",
    "regedit.exe",
    "uninstaller.exe",
}

_EXACT_RUNTIMES = {
    "wine",
    "wine64",
    "wine-preloader",
    "wine64-preloader",
    "wine64-preload",  # comm truncated at 15 bytes
    "umu-run",
    "umu_run.py",
    "gamemoderun",
    "lutris-wrapper",
}


@dataclass(frozen=True)
class ProcessView:
    """One process, reduced to the fields the matcher needs."""

    pid: int
    ppid: int
    names: frozenset[str]
    cmdline: str
    lutris_game: bool
    label: str


@dataclass(frozen=True)
class Match:
    pid: int
    label: str
    reason: str


class LaunchTracker:
    """Processes that appeared after the Lutris client was first observed.

    The first poll of a Lutris session is the baseline. Anything game-like
    that shows up later is treated as a launch, even if it is not a child
    of Lutris. When Lutris exits, the baseline is dropped.
    """

    def __init__(self) -> None:
        self._baselined = False
        self._baseline: set[int] = set()

    def game_pids(self, processes: list[ProcessView], lutris_running: bool) -> set[int]:
        live = {process.pid for process in processes}
        if not lutris_running:
            self._baselined = False
            self._baseline.clear()
            return set()
        if not self._baselined:
            self._baselined = True
            self._baseline = set(live)
            return set()
        by_pid = {process.pid: process for process in processes}
        appeared = {
            pid
            for pid in live - self._baseline
            if _looks_like_launched_game(by_pid[pid])
        }
        # Forget exited baseline processes so the set cannot grow forever.
        # New pids stay out of the baseline, so they remain "appeared".
        self._baseline &= live
        return appeared


def find_matches(
    processes: list[ProcessView],
    configured: list[str],
    lutris_new_pids: set[int] | None = None,
) -> list[Match]:
    """Return one match per interesting process.

    ``configured`` is the user's list. ``lutris_new_pids`` comes from
    :class:`LaunchTracker` and may be empty.
    """
    wanted = {name.strip().casefold() for name in configured if name.strip()}
    regular = wanted - {LUTRIS_NAME}
    watch_lutris = LUTRIS_NAME in wanted
    new_pids = lutris_new_pids or set()

    lutris_pids: set[int] = set()
    if watch_lutris:
        lutris_pids = {process.pid for process in processes if _is_lutris_client(process)}

    descendants: set[int] = set()
    if lutris_pids:
        children: dict[int, list[int]] = {}
        for process in processes:
            children.setdefault(process.ppid, []).append(process.pid)
        stack = list(lutris_pids)
        seen = set(lutris_pids)
        while stack:
            parent = stack.pop()
            for child in children.get(parent, []):
                if child in seen:
                    continue
                seen.add(child)
                descendants.add(child)
                stack.append(child)

    matches: list[Match] = []
    matched_pids: set[int] = set()

    for process in processes:
        hit = process.names & regular
        if hit:
            matches.append(Match(process.pid, process.label, "name"))
            matched_pids.add(process.pid)

    if not watch_lutris or not lutris_pids:
        return matches

    for process in processes:
        if process.pid in matched_pids or process.pid in lutris_pids:
            continue
        if process.lutris_game and not _is_wineserver(process) and not _is_only_wine_service(process):
            matches.append(Match(process.pid, process.label, "lutris-uuid"))
            matched_pids.add(process.pid)
            continue
        if process.pid in descendants and _meaningful_descendant(process):
            matches.append(Match(process.pid, process.label, "lutris-child"))
            matched_pids.add(process.pid)
            continue
        if process.pid in new_pids and _looks_like_launched_game(process):
            matches.append(Match(process.pid, process.label, "lutris-new"))
            matched_pids.add(process.pid)

    return matches


def take_snapshot(configured: list[str]) -> list[ProcessView]:
    """Read the process table. Reads game environment only while Lutris is up.

    ``LUTRIS_GAME_UUID`` is checked only when the watch list contains
    ``lutris`` and a Lutris client is actually running, so idle polling
    does not open every process's environment.
    """
    watch_lutris = any(name.strip().casefold() == LUTRIS_NAME for name in configured)
    views = _collect_processes(read_environ=False)
    if not watch_lutris or not any(_is_lutris_client(view) for view in views):
        return views
    flagged: list[ProcessView] = []
    for view in views:
        if _is_lutris_client(view):
            flagged.append(view)
            continue
        flagged.append(
            ProcessView(
                pid=view.pid,
                ppid=view.ppid,
                names=view.names,
                cmdline=view.cmdline,
                lutris_game=_environ_has_lutris_game(view.pid),
                label=view.label,
            )
        )
    return flagged


# Session helpers and tools that are poor choices in the "add an app" list.
# Matched against the short process name, so a few entries are the 15-byte
# comm truncation of a longer daemon name.
_PICKER_NOISE_PREFIXES = (
    "gsd-",
    "xdg-",
    "gvfs",
    "ibus-",
    "ibus",
    "evolution-",
    "gnome-session",
    "at-spi",
    "pipewire",
    "wireplumber",
    "dbus-",
    "systemd",
    "mutter",
    "goa-",
    "gcr-",
    "dconf",
    "rtkit",
    "fwupd",
    "gnome-shell",
    "gnome-keyring",
    "ssh-agent",
    "gpg-agent",
)

_PICKER_NOISE_EXACT = {
    "(sd-pam)",
    "bash",
    "sh",
    "dash",
    "zsh",
    "fish",
    "ps",
    "grep",
    "sed",
    "awk",
    "cat",
    "sleep",
    "bwrap",
    "gio",
    "gjs",
    "fusermount",
    "fusermount3",
    "xdg-dbus-proxy",
}


def list_running_app_names() -> list[str]:
    """Process names worth offering in the add-app picker."""
    return candidate_app_names(take_snapshot([]))


def candidate_app_names(processes: list[ProcessView]) -> list[str]:
    """Unique labels from ``processes``, with session noise removed.

    The label is the same string the watcher matches: an ``.exe`` when the
    command line has one, otherwise the executable or process name.
    """
    own_pid = os.getpid()
    found: dict[str, str] = {}
    for process in processes:
        if process.pid == own_pid or _is_picker_noise(process):
            continue
        label = process.label.strip()
        if not label:
            continue
        found.setdefault(label.casefold(), label)
    return sorted(found.values(), key=str.casefold)


def _is_picker_noise(process: ProcessView) -> bool:
    if "scale_switcher.py" in process.cmdline:
        return True
    if _is_wineserver(process) or _is_only_wine_service(process):
        return True
    # A Wine game's command line also contains wine. Keep it when a real
    # .exe is present; candidate_app_names then shows that .exe.
    if any(name.endswith(".exe") and name not in _WINE_SERVICES for name in process.names):
        return False
    names = set(process.names)
    if process.label:
        names.add(process.label.casefold())
    return any(_is_noise_name(name) for name in names)


def _is_noise_name(name: str) -> bool:
    if name in _PICKER_NOISE_EXACT or _is_python(name):
        return True
    return any(name.startswith(prefix) for prefix in _PICKER_NOISE_PREFIXES)


def _collect_processes(read_environ: bool) -> list[ProcessView]:
    views: list[ProcessView] = []
    try:
        iterator = psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline"])
    except (psutil.Error, OSError) as exc:
        log.error("Could not list processes: %s", exc)
        return views
    for proc in iterator:
        try:
            info = proc.info
            pid = int(info["pid"])
            ppid = int(info["ppid"] or 0)
            names: set[str] = set()
            comm = info.get("name") or ""
            if comm:
                names.add(comm.casefold())
            exe_base = _basename(info.get("exe"))
            if exe_base:
                names.add(exe_base.casefold())
            cmdline_list = info.get("cmdline") or []
            if cmdline_list and cmdline_list[0]:
                argv0 = _basename(str(cmdline_list[0]))
                if argv0:
                    names.add(argv0.casefold())
            exe_label = None
            for token in cmdline_list:
                base = _basename(str(token))
                if not base:
                    continue
                folded = base.casefold()
                # `python3 /usr/bin/lutris` keeps the script name in argv,
                # not in comm. An exact basename match avoids treating a
                # path like `.../lutris/runners/wine` as the client.
                if folded == LUTRIS_NAME or folded.endswith(".exe"):
                    names.add(folded)
                # The last real .exe on the command line is the game. Wine's
                # own tools (explorer.exe, services.exe, …) are not a label.
                if folded.endswith(".exe") and folded not in _WINE_SERVICES:
                    exe_label = base
            cmdline = " ".join(str(token) for token in cmdline_list).casefold()[:400]
            label = exe_label or exe_base or comm or str(pid)
            lutris_game = _environ_has_lutris_game(pid) if read_environ else False
            views.append(
                ProcessView(
                    pid=pid,
                    ppid=ppid,
                    names=frozenset(names),
                    cmdline=cmdline,
                    lutris_game=lutris_game,
                    label=label,
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except (OSError, ValueError) as exc:
            log.debug("Skipping a process that disappeared: %s", exc)
            continue
    return views


def _basename(path: str | None) -> str | None:
    if not path:
        return None
    name = str(path).rstrip("/").split("/")[-1].strip()
    return name or None


def _environ_has_lutris_game(pid: int) -> bool:
    """True when the process environment contains LUTRIS_GAME_UUID.

    The environment is scanned as raw bytes and the value is never logged.
    """
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            data = handle.read()
    except OSError:
        return False
    return b"\0LUTRIS_GAME_UUID=" in data or data.startswith(b"LUTRIS_GAME_UUID=")


def _is_lutris_client(process: ProcessView) -> bool:
    if LUTRIS_NAME in process.names:
        return True
    # Flatpak Lutris shows up on the host as bwrap with this app id.
    return "net.lutris.lutris" in process.cmdline


def _is_python(name: str) -> bool:
    return name == "python" or name.startswith("python3")


def _is_client_helper(process: ProcessView) -> bool:
    if not process.names:
        return True
    return all(_is_helper_name(name) for name in process.names)


def _is_helper_name(name: str) -> bool:
    return name in _CLIENT_HELPERS or _is_python(name)


def _is_wineserver(process: ProcessView) -> bool:
    return any(name == "wineserver" or name.startswith("wineserver") for name in process.names)


def _is_wine_service_name(name: str) -> bool:
    return name in _WINE_SERVICES


def _is_only_wine_service(process: ProcessView) -> bool:
    if not process.names:
        return False
    return all(_is_wine_service_name(name) or _is_wineserver_name(name) for name in process.names)


def _is_wineserver_name(name: str) -> bool:
    return name == "wineserver" or name.startswith("wineserver")


def _is_runtime_name(name: str) -> bool:
    if name in _EXACT_RUNTIMES:
        return True
    return (
        name.startswith("gamescope")
        or name.startswith("pressure-vessel")
        or name.startswith("pressure-vesse")
        or name.startswith("umu-run")
        or name.startswith("umu_run")
    )


def _looks_like_launched_game(process: ProcessView) -> bool:
    """Game-like process used for the 'appeared after Lutris' signal."""
    if _is_client_helper(process) or _is_wineserver(process) or _is_only_wine_service(process):
        return False
    for name in process.names:
        if name.endswith(".exe") and name not in _WINE_SERVICES:
            return True
        if _is_runtime_name(name):
            return True
    return False


def _meaningful_descendant(process: ProcessView) -> bool:
    """A Lutris child that is the game (or the wrapper that tracks it)."""
    if _is_client_helper(process) or _is_wineserver(process):
        return False
    interesting = False
    for name in process.names:
        if _is_wine_service_name(name) or _is_wineserver_name(name) or _is_helper_name(name):
            continue
        interesting = True
        break
    return interesting


class Watcher:
    """Poll processes and ask the scaler to change on stable transitions.

    Runs on the GLib main loop. ``on_status`` is called every poll with a
    dict the window can render. Scale changes happen on that same thread.
    """

    def __init__(
        self,
        config: Config,
        state: RuntimeState,
        scaler: DisplayScaler,
        notify,
        on_status,
    ) -> None:
        self.config = config
        self.state = state
        self.scaler = scaler
        self._notify = notify
        self._on_status = on_status
        self.latched_gaming = state.gaming_mode
        self.pending_since: float | None = None
        self.pending_gaming: bool | None = None
        self.matches: list[Match] = []
        self.current_scale: float | None = None
        self._timeout_id: int | None = None
        self._launches = LaunchTracker()
        self._fail_count = 0
        self._last_logged_labels: tuple[str, ...] | None = None
        # After a restart the saved latch can say "game mode" while Mutter
        # has already returned to the desktop scale. Reconcile that once.
        self._needs_reconcile = True

    def start(self) -> None:
        from gi.repository import GLib

        if self._timeout_id is not None:
            return
        interval_ms = int(self.config.poll_interval_seconds * 1000)
        self._timeout_id = GLib.timeout_add(interval_ms, self._on_timeout)
        log.info(
            "Watcher started (poll %.1fs, debounce %.1fs)",
            self.config.poll_interval_seconds,
            self.config.debounce_seconds,
        )
        self._on_timeout()

    def stop(self) -> None:
        from gi.repository import GLib

        if self._timeout_id is None:
            return
        GLib.source_remove(self._timeout_id)
        self._timeout_id = None
        log.info("Watcher stopped")

    def _on_timeout(self) -> bool:
        try:
            self.poll()
        except Exception:
            log.exception("Process poll failed")
        return True

    def poll(self, now: float | None = None) -> None:
        """One detection cycle. ``now`` is injectable for tests."""
        import time

        if now is None:
            now = time.monotonic()
        try:
            processes = take_snapshot(self.config.processes)
        except Exception:
            log.exception("Process snapshot failed")
            processes = []
        lutris_running = any(name.strip().casefold() == LUTRIS_NAME for name in self.config.processes) and any(
            _is_lutris_client(process) for process in processes
        )
        new_pids = self._launches.game_pids(processes, lutris_running)
        self.matches = find_matches(processes, self.config.processes, new_pids)
        self._apply_decision(now)
        self._publish()

    def _apply_decision(self, now: float) -> None:
        gaming = bool(self.matches)
        self._log_match_changes()
        self._reconcile_after_restart(gaming)
        if not self.config.enabled:
            self.pending_since = None
            self.pending_gaming = None
            if self.latched_gaming:
                self._switch(False, reason="watcher disabled", now=now)
            return

        if gaming == self.latched_gaming:
            if self.pending_gaming is not None:
                log.info("Scale change cancelled; the set of monitored apps changed again.")
            self.pending_since = None
            self.pending_gaming = None
            self._fail_count = 0
            return

        if self.pending_gaming != gaming:
            self.pending_gaming = gaming
            self.pending_since = now
            if gaming:
                log.info(
                    "Monitored app started (%s). Waiting %.1fs before setting %s",
                    _format_labels(self.matches),
                    self.config.debounce_seconds,
                    format_percent(GAME_SCALE),
                )
            else:
                log.info(
                    "Last monitored app exited. Waiting %.1fs before restoring %s",
                    self.config.debounce_seconds,
                    format_percent(NORMAL_SCALE),
                )
            if self.config.debounce_seconds > 0:
                return

        assert self.pending_since is not None
        if now - self.pending_since < self.config.debounce_seconds:
            return
        self._switch(gaming, reason="debounced", now=now)

    def _reconcile_after_restart(self, gaming: bool) -> None:
        """If a game is still running but the scale is not 100%, switch again.

        Temporary Mutter configs usually survive our process exiting. This
        covers the case where they did not, or the first apply never landed,
        while ``state.json`` still says a game session is active.
        """
        if not self._needs_reconcile:
            return
        if not (self.config.enabled and gaming and self.latched_gaming):
            self._needs_reconcile = False
            return
        scale = self.scaler.get_primary_scale()
        if scale is None:
            return
        self._needs_reconcile = False
        if scales_close(scale, GAME_SCALE):
            return
        log.info(
            "A monitored app is already running and the scale is %s. Setting %s.",
            format_percent(scale),
            format_percent(GAME_SCALE),
        )
        # Pretend we are still at the desktop scale so the normal transition
        # path applies 100% after the debounce.
        self.latched_gaming = False

    def _switch(self, gaming: bool, reason: str, now: float) -> None:
        target = GAME_SCALE if gaming else NORMAL_SCALE
        ok = self.scaler.set_scale(target)
        if not ok:
            self._fail_count += 1
            # Wait another full debounce before trying again so a stuck
            # display server does not get a request on every poll.
            self.pending_since = now
            if self._fail_count == 1 or self._fail_count % 15 == 0:
                log.error(
                    "Scale change to %s failed (%s). Will retry. %s",
                    format_percent(target),
                    reason,
                    self.scaler.last_error or "",
                )
            return
        self._fail_count = 0
        self.latched_gaming = gaming
        self.pending_since = None
        self.pending_gaming = None
        self.state.set_gaming_mode(gaming)
        if gaming:
            title = f"Display scale set to {format_percent(GAME_SCALE)}"
            body = f"Running: {_format_labels(self.matches)}"
        elif reason == "watcher disabled":
            title = f"Display scale restored to {format_percent(NORMAL_SCALE)}"
            body = "The watcher was turned off."
        else:
            title = f"Display scale restored to {format_percent(NORMAL_SCALE)}"
            body = "No monitored apps are running."
        log.info("%s (%s)", title, reason)
        self._notify(title, body, self.config.notifications)

    def _log_match_changes(self) -> None:
        labels = tuple(sorted({match.label for match in self.matches}))
        if labels == self._last_logged_labels:
            return
        self._last_logged_labels = labels
        if labels:
            log.info("Monitored apps running: %s", ", ".join(labels))
        else:
            log.info("No monitored apps running")

    def _publish(self) -> None:
        self.current_scale = self.scaler.get_primary_scale()
        pending = (
            self.pending_gaming is not None
            and self.pending_gaming != self.latched_gaming
            and self.config.enabled
        )
        status = {
            "enabled": self.config.enabled,
            "gaming": bool(self.matches),
            "latched_gaming": self.latched_gaming,
            "pending": pending,
            "pending_gaming": self.pending_gaming,
            "scale": self.current_scale,
            "matches": [match.label for match in self.matches],
            "error": self.scaler.last_error,
        }
        try:
            self._on_status(status)
        except Exception:
            log.exception("Status callback failed")

    def test_scale(self, target: float) -> bool:
        """Apply a scale immediately from the test buttons.

        Does not change the latched game/idle state. While the watcher is
        enabled it will put the scale back on the next stable poll if the
        test value disagrees with whether a monitored app is running.
        """
        ok = self.scaler.set_scale(target)
        if ok:
            if scales_close(target, GAME_SCALE):
                title = f"Display scale set to {format_percent(GAME_SCALE)}"
            else:
                title = f"Display scale restored to {format_percent(NORMAL_SCALE)}"
            self._notify(title, "Manual test.", self.config.notifications)
        self._publish()
        return ok


def _format_labels(matches: list[Match]) -> str:
    labels = []
    for match in matches:
        if match.label not in labels:
            labels.append(match.label)
    if not labels:
        return "unknown"
    shown = ", ".join(labels[:3])
    if len(labels) > 3:
        shown += f" (+{len(labels) - 3} more)"
    return shown
