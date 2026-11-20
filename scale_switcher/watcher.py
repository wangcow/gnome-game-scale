"""Find monitored processes, with extra rules for Lutris.

Matching
--------
A configured name matches a process when it equals, case-insensitively, any of:

* ``/proc/<pid>/comm`` (Linux truncates this to 15 bytes)
* the executable basename
* ``argv[0]``'s basename
* any ``*.exe`` on the command line. Wine shows Windows paths there
  (``C:\\Program Files (x86)\\Battle.net\\Battle.net.exe``), so the file
  name is taken after the last ``\\`` or ``/``.

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

Delays
------
The scale changes only after the "any game / no game" answer has been
stable for a while: ``start_delay_seconds`` before 100%, and the longer
``stop_delay_seconds`` before the desktop scale comes back. One game
closing while another is still open does not restore the desktop scale.

Holding 100%
------------
Mutter can drop the temporary 100% on its own: a monitor plugged in or
out, the lid, resume from suspend, or Settings → Displays. When Mutter
reports a monitor change while a monitored app is running, the watcher
waits ``start_delay_seconds`` and sets 100% again.

Scale changes are asynchronous: Mutter answers only after the modeset,
and the main loop keeps running meanwhile. One change is in flight at a
time; its result is latched when Mutter answers.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import psutil
from gi.repository import GLib

from scale_switcher.config import Config, RuntimeState
from scale_switcher.scale import (
    GAME_SCALE,
    DisplayScaler,
    format_percent,
    merge_restore_scales,
)

log = logging.getLogger("scale_switcher")

LUTRIS_NAME = "lutris"

# After a failed scale change, wait this long before asking Mutter again.
RETRY_SECONDS = 5.0

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
    "gamemoderun",
    "lutris-wrapper",
}
_RUNTIME_PREFIXES = ("gamescope", "pressure-vesse", "umu-run", "umu_run")

# An ``.exe`` file name inside a command line. After a backslash the name
# may contain spaces (``C:\Games\World of Warcraft Launcher.exe``). A bare
# token or a Unix path component may not, so ``/usr/bin/wine game.exe``
# yields ``game.exe`` rather than ``wine game.exe``.
_EXE_RE = re.compile(
    r"""(?:\\([^\\/]+?\.exe)
         |(?:^|(?<=[\s/]))([^\\/\s]+?\.exe))
        (?=[\s"']|$)""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class ProcessView:
    """One process, reduced to the fields the matcher needs."""

    pid: int
    ppid: int
    names: frozenset[str]
    cmdline: str
    lutris_game: bool
    label: str
    # Real user id; -1 when unknown.
    uid: int = -1


@dataclass(frozen=True)
class Match:
    pid: int
    label: str
    reason: str


@dataclass(frozen=True)
class WatcherStatus:
    """What the window shows on the status row."""

    enabled: bool
    # The scale change waiting for its delay: True for 100%, False for
    # the desktop scale, None when nothing is queued.
    pending_gaming: bool | None
    scale: float | None
    labels: tuple[str, ...]
    error: str | None


class LaunchTracker:
    """Processes that appeared after the Lutris client was first observed.

    The first poll of a Lutris session is the baseline. Anything game-like
    that shows up later is treated as a launch, even if it is not a child
    of Lutris. When Lutris exits, the baseline is dropped.
    """

    def __init__(self) -> None:
        self._baseline: set[int] | None = None

    def game_pids(self, processes: list[ProcessView], lutris_running: bool) -> set[int]:
        if not lutris_running:
            self._baseline = None
            return set()
        by_pid = {process.pid: process for process in processes}
        if self._baseline is None:
            self._baseline = set(by_pid)
            return set()
        appeared = {
            pid for pid in by_pid.keys() - self._baseline if _looks_like_launched_game(by_pid[pid])
        }
        # Forget exited baseline processes so the set cannot grow forever.
        # New pids stay out of the baseline, so they remain "appeared".
        self._baseline &= by_pid.keys()
        return appeared


def watches_lutris(configured: list[str]) -> bool:
    return any(name.strip().casefold() == LUTRIS_NAME for name in configured)


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

    matches = [Match(p.pid, p.label, "name") for p in processes if p.names & regular]
    matched_pids = {match.pid for match in matches}

    lutris_pids = (
        {p.pid for p in processes if _is_lutris_client(p)} if LUTRIS_NAME in wanted else set()
    )
    if not lutris_pids:
        return matches

    descendants = _descendants(processes, lutris_pids)
    new_pids = lutris_new_pids or set()
    for process in processes:
        if process.pid in matched_pids or process.pid in lutris_pids:
            continue
        if _is_client_helper(process) or _is_wine_service(process):
            continue
        if process.lutris_game:
            reason = "lutris-uuid"
        elif process.pid in descendants:
            reason = "lutris-child"
        elif process.pid in new_pids and _looks_like_launched_game(process):
            reason = "lutris-new"
        else:
            continue
        matches.append(Match(process.pid, process.label, reason))
    return matches


def _descendants(processes: list[ProcessView], roots: set[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for process in processes:
        children.setdefault(process.ppid, []).append(process.pid)
    found: set[int] = set()
    stack = list(roots)
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found and child not in roots:
                found.add(child)
                stack.append(child)
    return found


def take_snapshot(configured: list[str]) -> list[ProcessView]:
    """Read the process table. Reads game environment only while Lutris is up.

    ``LUTRIS_GAME_UUID`` is checked only when the watch list contains
    ``lutris`` and a Lutris client is actually running, so idle polling
    does not open every process's environment.
    """
    views = _collect_processes()
    if not watches_lutris(configured) or not any(_is_lutris_client(view) for view in views):
        return views
    return [
        view
        if _is_lutris_client(view)
        else dataclasses.replace(view, lutris_game=_environ_has_lutris_game(view.pid))
        for view in views
    ]


# Session helpers and tools that are poor choices in the "add an app" list.
# Matched against the short process name, so a few entries are the 15-byte
# comm truncation of a longer daemon name.
_PICKER_NOISE_PREFIXES = (
    "gsd-",
    "xdg-",
    "gvfs",
    "ibus",
    "evolution-",
    "gnome-session",
    "gnome-shell",
    "gnome-keyring",
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
    "ssh-agent",
    "gpg-agent",
    "gdm-",
    "flatpak-",
    "localsearch",
    "p11-kit",
    "glycin",
    "xwayland",
    "uresourced",
    "catatonit",
    "obexd",
    "ptyxis-agent",
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
    own_uid = os.getuid()
    found: dict[str, str] = {}
    for process in processes:
        # Games run as the user. Root daemons and kernel threads are noise.
        if process.uid != own_uid or process.pid == own_pid or _is_picker_noise(process):
            continue
        label = process.label.strip()
        if label:
            found.setdefault(label.casefold(), label)
    return sorted(found.values(), key=str.casefold)


def _is_picker_noise(process: ProcessView) -> bool:
    if "scale_switcher.py" in process.cmdline or _is_wine_service(process):
        return True
    # A Wine game's command line also contains wine. Keep it when a real
    # .exe is present; candidate_app_names then shows that .exe.
    if _game_exes(process):
        return False
    return any(_is_noise_name(name) for name in process.names | {process.label.casefold()})


def _is_noise_name(name: str) -> bool:
    if name in _PICKER_NOISE_EXACT or _is_python(name):
        return True
    return name.startswith(_PICKER_NOISE_PREFIXES)


def exe_names(cmdline: str) -> list[str]:
    """``.exe`` file names on a command line, in order."""
    return [windows or plain for windows, plain in _EXE_RE.findall(cmdline)]


def _collect_processes() -> list[ProcessView]:
    views: list[ProcessView] = []
    try:
        iterator = psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline", "uids"])
    except (psutil.Error, OSError) as exc:
        log.error("Could not list processes: %s", exc)
        return views
    for proc in iterator:
        try:
            info = proc.info
            pid = int(info["pid"])
            comm = info.get("name") or ""
            exe_base = _basename(info.get("exe"))
            cmdline_list = [str(token) for token in info.get("cmdline") or []]
            names = {name.casefold() for name in (comm, exe_base) if name}
            if cmdline_list:
                argv0 = _basename(cmdline_list[0])
                if argv0:
                    names.add(argv0.casefold())
            # `python3 /usr/bin/lutris` keeps the script name in argv, not in
            # comm. An exact basename match avoids treating a path like
            # `.../lutris/runners/wine` as the client.
            if any((_basename(token) or "").casefold() == LUTRIS_NAME for token in cmdline_list):
                names.add(LUTRIS_NAME)
            joined = " ".join(cmdline_list)
            exe_label = None
            for exe in exe_names(joined):
                names.add(exe.casefold())
                # The last real .exe on the command line is the game. Wine's
                # own tools (explorer.exe, services.exe, …) are not a label.
                if exe.casefold() not in _WINE_SERVICES:
                    exe_label = exe
            views.append(
                ProcessView(
                    pid=pid,
                    ppid=int(info["ppid"] or 0),
                    names=frozenset(names),
                    cmdline=joined.casefold()[:400],
                    lutris_game=False,
                    label=exe_label or exe_base or comm or str(pid),
                    uid=info["uids"].real if info.get("uids") else -1,
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
    name = str(path).replace("\\", "/").rstrip("/").split("/")[-1].strip()
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
    # Flatpak Lutris shows up on the host as bwrap with this app id.
    return LUTRIS_NAME in process.names or "net.lutris.lutris" in process.cmdline


def _is_python(name: str) -> bool:
    return name == "python" or name.startswith("python3")


def _is_client_helper(process: ProcessView) -> bool:
    return all(name in _CLIENT_HELPERS or _is_python(name) for name in process.names)


def _game_exes(process: ProcessView) -> set[str]:
    return {name for name in process.names if name.endswith(".exe") and name not in _WINE_SERVICES}


def _is_wine_service(process: ProcessView) -> bool:
    """wineserver, or a process whose only .exe names are Wine's own tools.

    Under Wine the executable is the preloader, so a service process also
    carries ``wine64-preloader`` in its names. Judge it by its .exe.
    """
    if any(name.startswith("wineserver") for name in process.names):
        return True
    exes = {name for name in process.names if name.endswith(".exe")}
    return bool(exes) and exes <= _WINE_SERVICES


def _is_runtime_name(name: str) -> bool:
    return name in _EXACT_RUNTIMES or name.startswith(_RUNTIME_PREFIXES)


def _looks_like_launched_game(process: ProcessView) -> bool:
    """Game-like process used for the 'appeared after Lutris' signal."""
    if _is_client_helper(process) or _is_wine_service(process):
        return False
    return bool(_game_exes(process)) or any(_is_runtime_name(name) for name in process.names)


def format_labels(labels: list[str] | tuple[str, ...], limit: int = 3) -> str:
    unique = list(dict.fromkeys(labels))
    if not unique:
        return "unknown"
    shown = ", ".join(unique[:limit])
    if len(unique) > limit:
        shown += f" (+{len(unique) - limit} more)"
    return shown


class Watcher:
    """Poll processes and ask the scaler to change on stable transitions.

    Runs on the GLib main loop. ``on_status`` is called every poll with a
    :class:`WatcherStatus`. Scale changes happen on that same thread.
    """

    def __init__(
        self,
        config: Config,
        state: RuntimeState,
        scaler: DisplayScaler,
        notify: Callable[[str, str, bool], None],
        on_status: Callable[[WatcherStatus], None],
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
        self._timeout_id: int | None = None
        self._launches = LaunchTracker()
        self._fail_count = 0
        # No scale change before this time, after a failure.
        self._retry_at: float | None = None
        # True while Mutter is applying a change this watcher asked for.
        self._busy = False
        self._last_logged_labels: tuple[str, ...] | None = None
        # When the display may have left 100% while a game runs. Starts in
        # the past so the first poll checks: after a restart the saved latch
        # can say "game mode" while Mutter is back at the desktop scale.
        self._drift_since: float | None = -math.inf
        scaler.on_monitors_changed = self._on_monitors_changed

    def start(self) -> None:
        if self._timeout_id is not None:
            return
        interval_ms = int(self.config.poll_interval_seconds * 1000)
        self._timeout_id = GLib.timeout_add(interval_ms, self._on_timeout)
        log.info(
            "Watcher started (poll %.1fs, start delay %.1fs, stop delay %.1fs)",
            self.config.poll_interval_seconds,
            self.config.start_delay_seconds,
            self.config.stop_delay_seconds,
        )
        self._on_timeout()

    def stop(self) -> None:
        if self._timeout_id is None:
            return
        GLib.source_remove(self._timeout_id)
        self._timeout_id = None
        log.info("Watcher stopped")

    def restore_on_exit(self) -> None:
        """Put the desktop scale back if this app left the display at 100%."""
        if not (self.latched_gaming or self.state.restore_scales):
            return
        log.info("Restoring the desktop scale before exit")
        # Short timeout: at logout Mutter may already be going away. The
        # state file keeps the saved scales for the next start if this fails.
        if self.scaler.restore_scales_blocking(self.state.restore_scales, timeout_ms=3000):
            self.latched_gaming = False
            self.state.set_restore_scales({})
            self.state.set_gaming_mode(False)

    def _on_timeout(self) -> bool:
        try:
            self.poll()
        except Exception:
            log.exception("Process poll failed")
        return GLib.SOURCE_CONTINUE

    def _on_monitors_changed(self) -> None:
        if self._drift_since is None:
            self._drift_since = time.monotonic()

    def poll(self, now: float | None = None) -> None:
        """One detection cycle. ``now`` is injectable for tests."""
        if now is None:
            now = time.monotonic()
        processes = take_snapshot(self.config.processes)
        lutris_running = watches_lutris(self.config.processes) and any(
            _is_lutris_client(process) for process in processes
        )
        new_pids = self._launches.game_pids(processes, lutris_running)
        self.matches = find_matches(processes, self.config.processes, new_pids)
        self._log_match_changes()
        self._apply_decision(now)
        self._hold_game_scale(now)
        self._publish()

    def _clear_pending(self) -> None:
        self.pending_since = None
        self.pending_gaming = None
        self._retry_at = None

    def _delay_for(self, gaming: bool) -> float:
        return self.config.start_delay_seconds if gaming else self.config.stop_delay_seconds

    def _retry_wait(self, now: float) -> bool:
        return self._retry_at is not None and now < self._retry_at

    def _apply_decision(self, now: float) -> None:
        gaming = bool(self.matches)
        if not self.config.enabled:
            self._clear_pending()
            if self.latched_gaming:
                self._switch(False, reason="watcher disabled", now=now)
            return

        if gaming == self.latched_gaming:
            if self.pending_gaming is not None:
                log.info("Scale change cancelled; the set of monitored apps changed again.")
            self._clear_pending()
            self._fail_count = 0
            return

        delay = self._delay_for(gaming)
        if self.pending_gaming != gaming:
            self.pending_gaming = gaming
            self.pending_since = now
            self._retry_at = None
            if gaming:
                log.info(
                    "Monitored app started (%s). Waiting %.1fs before setting %s",
                    format_labels([match.label for match in self.matches]),
                    delay,
                    format_percent(GAME_SCALE),
                )
            else:
                log.info(
                    "Last monitored app exited. Waiting %.1fs before restoring the desktop scale",
                    delay,
                )

        assert self.pending_since is not None
        if now - self.pending_since >= delay and not self._retry_wait(now):
            self._switch(gaming, reason="delay elapsed", now=now)

    def _hold_game_scale(self, now: float) -> None:
        """Set 100% again if Mutter changed the display during a game."""
        if self._drift_since is None:
            return
        holding = (
            self.config.enabled
            and self.latched_gaming
            and bool(self.matches)
            and self.pending_gaming is None
        )
        if not holding:
            self._drift_since = None
            return
        # Let Mutter finish a hotplug or resume (or our own change) first.
        if now - self._drift_since < self.config.start_delay_seconds or self._retry_wait(now) or self._busy:
            return
        state = self.scaler.get_state(fresh=True)
        if state is None:
            self._drift_since = now
            return
        if state.all_at(GAME_SCALE):
            self._drift_since = None
            return
        log.info(
            "The display left %s while a monitored app is running (now %s). Setting it again.",
            format_percent(GAME_SCALE),
            format_percent(state.primary_scale or 0),
        )
        # On failure _finish_switch sets _drift_since again for a retry.
        self._drift_since = None
        self._switch(True, reason="reapplied", now=now)

    def _enter_game_scale(self, on_done: Callable[[bool], None]) -> None:
        """Remember each monitor's desktop scale, then set 100%."""
        state = self.scaler.get_state(fresh=True)
        if state is None:
            on_done(False)
            return
        merged = merge_restore_scales(self.state.restore_scales, state.scales_by_key())
        if merged != self.state.restore_scales:
            self.state.set_restore_scales(merged)
        self.scaler.set_scale(GAME_SCALE, on_done)

    def _restore_desktop_scale(self, on_done: Callable[[bool], None]) -> None:
        def restored(ok: bool) -> None:
            if ok:
                self.state.set_restore_scales({})
            on_done(ok)

        self.scaler.restore_scales(self.state.restore_scales, restored)

    def _run_change(self, gaming: bool, on_done: Callable[[bool], None]) -> bool:
        """Start a scale change unless one is already being applied.

        ``on_done`` runs when Mutter answers. Returns False when busy.
        """
        if self._busy:
            return False
        self._busy = True

        def finished(ok: bool) -> None:
            self._busy = False
            on_done(ok)
            self._publish()

        if gaming:
            self._enter_game_scale(finished)
        else:
            self._restore_desktop_scale(finished)
        return True

    def _switch(self, gaming: bool, reason: str, now: float) -> None:
        """Change the scale for a settled transition; the result latches."""
        self._run_change(gaming, lambda ok: self._finish_switch(gaming, reason, now, ok))

    def _finish_switch(self, gaming: bool, reason: str, now: float, ok: bool) -> None:
        if not ok:
            self._fail_count += 1
            # A stuck display server should not get a request on every poll.
            self._retry_at = now + RETRY_SECONDS
            if reason == "reapplied":
                self._drift_since = now
            if self._fail_count == 1 or self._fail_count % 15 == 0:
                log.error(
                    "Scale change failed (%s). Will retry. %s",
                    reason,
                    self.scaler.last_error or "",
                )
            return
        # The game may have exited while Mutter was applying 100%. Latching
        # what the display now shows lets the next poll start the restore.
        self._fail_count = 0
        self.latched_gaming = gaming
        self._clear_pending()
        self.state.set_gaming_mode(gaming)
        if gaming:
            title = f"Display scale set to {format_percent(GAME_SCALE)}"
            running = format_labels([match.label for match in self.matches])
            if reason == "reapplied":
                body = f"The display settings changed while {running} is running."
            else:
                body = f"Running: {running}"
        else:
            title = "Desktop display scale restored"
            if reason == "watcher disabled":
                body = "The watcher was turned off."
            else:
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
        pending = self.pending_gaming if self.config.enabled else None
        status = WatcherStatus(
            enabled=self.config.enabled,
            pending_gaming=pending,
            scale=self.scaler.get_primary_scale(),
            labels=tuple(dict.fromkeys(match.label for match in self.matches)),
            error=self.scaler.last_error,
        )
        try:
            self._on_status(status)
        except Exception:
            log.exception("Status callback failed")

    def test_game_scale(self) -> None:
        """Test button: set 100% now, remembering the desktop scale."""

        def done(ok: bool) -> None:
            if ok:
                title = f"Display scale set to {format_percent(GAME_SCALE)}"
                self._notify(title, "Manual test.", self.config.notifications)

        self._run_test(True, done)

    def test_restore(self) -> None:
        """Test button: put the remembered desktop scale back now.

        While a monitored app is running the watcher sets 100% again.
        """

        def done(ok: bool) -> None:
            if ok:
                self._notify("Desktop display scale restored", "Manual test.", self.config.notifications)
            # Treat it like any other display change, so a running game
            # gets 100% back after the start delay.
            self._on_monitors_changed()

        self._run_test(False, done)

    def _run_test(self, gaming: bool, on_done: Callable[[bool], None]) -> None:
        if not self._run_change(gaming, on_done):
            log.info("A scale change is still being applied; try again in a moment.")
