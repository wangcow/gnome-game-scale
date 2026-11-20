"""Read and change the GNOME display scale through Mutter.

Mutter exposes ``org.gnome.Mutter.DisplayConfig`` on the session bus.
``GetCurrentState`` returns every logical monitor; ``ApplyMonitorsConfig``
applies a new configuration. Resolution, rotation, and which monitor is
primary are kept as they are.

The apply method used here is *temporary* (method 1). The scale changes
immediately in the running session and ``~/.config/monitors.xml`` is left
alone, so the next login still starts from the scale saved in Settings.

Each logical monitor gets its own target scale. Entering game mode sets
every monitor to 100%. Leaving it puts back the scale each monitor had
before (see :func:`restore_target`), so a laptop at 125% next to an
external monitor at 100% comes back exactly as it was.

In Mutter's logical layout mode, monitor positions are in logical pixels,
so changing one monitor's scale changes its size. Monitors placed after it
are moved by the same amount (see :func:`relayout`) so they stay adjacent
instead of overlapping.

The current state is cached and dropped when Mutter emits
``MonitorsChanged``, so the once-a-second status refresh does not cost a
D-Bus round trip.

Before any call, the desktop session is identified (``platform.py``).
Mutter is used only when that session is GNOME.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from gi.repository import Gio, GLib

from scale_switcher.platform import Platform, current_platform

log = logging.getLogger("scale_switcher")

# The scale used while a monitored app is running.
GAME_SCALE = 1.0
# Used on restore only for a monitor that sits at GAME_SCALE and has no
# saved scale (for example a state file written by an older version).
FALLBACK_DESKTOP_SCALE = 1.25

# ApplyMonitorsConfig method: 1 applies without writing monitors.xml.
METHOD_TEMPORARY = 1
# DisplayConfig layout-mode property.
LAYOUT_MODE_LOGICAL = 1

BUS_NAME = "org.gnome.Mutter.DisplayConfig"
OBJECT_PATH = "/org/gnome/Mutter/DisplayConfig"
INTERFACE = "org.gnome.Mutter.DisplayConfig"

# How close two scale factors have to be to count as the same setting.
# Supported-scale lists sometimes contain values like 1.3333333730697632.
SCALE_EPSILON = 0.02

# Reject a "closest supported scale" that is this far from the target,
# so 1.25 is never silently snapped to 1.5.
MAX_SCALE_SNAP = 0.05


def format_percent(scale: float) -> str:
    """Format 1.25 as '125%'."""
    return f"{round(scale * 100)}%"


def scales_close(left: float, right: float) -> bool:
    return abs(left - right) <= SCALE_EPSILON


def snap_scale(supported: list[float], target: float) -> float | None:
    """Pick Mutter's advertised scale closest to ``target``.

    Passing the exact value from the mode's supported-scale list avoids
    rejections caused by binary float formatting. Returns None when nothing
    advertised is close enough. An empty list means the mode did not
    advertise scales, so ``target`` is passed through.
    """
    if not supported:
        return target
    best = min(supported, key=lambda item: abs(item - target))
    if abs(best - target) > MAX_SCALE_SNAP:
        return None
    return float(best)


@dataclass
class LogicalMonitor:
    x: int
    y: int
    scale: float
    transform: int
    primary: bool
    # (connector, mode id, per-monitor options as plain numbers)
    monitors: list[tuple[str, str, dict]] = field(default_factory=list)
    supported_scales: list[list[float]] = field(default_factory=list)
    # Physical pixel size of the current mode of the first monitor.
    mode_size: tuple[int, int] = (0, 0)
    # Stable identity (vendor/product/serial), used to remember its scale.
    key: str = ""

    @property
    def connectors(self) -> str:
        return ", ".join(item[0] for item in self.monitors) or "unknown"

    def logical_size(self, scale: float, layout_mode: int | None) -> tuple[int, int]:
        width, height = self.mode_size
        if self.transform % 2:
            # 90 and 270 degree rotations, flipped or not, swap the axes.
            width, height = height, width
        if layout_mode == LAYOUT_MODE_LOGICAL and scale > 0:
            # Mutter rounds the same way (roundf).
            width = int(width / scale + 0.5)
            height = int(height / scale + 0.5)
        return width, height


@dataclass
class DisplayState:
    serial: int
    layout_mode: int | None
    supports_layout_mode: bool
    logical_monitors: list[LogicalMonitor]
    # Monitors Mutter currently offers for DRM lease, preserved on apply.
    for_lease: list[tuple[str, str, str, str]] = field(default_factory=list)

    @property
    def primary_scale(self) -> float | None:
        if not self.logical_monitors:
            return None
        for monitor in self.logical_monitors:
            if monitor.primary:
                return monitor.scale
        return self.logical_monitors[0].scale

    def all_at(self, target: float) -> bool:
        if not self.logical_monitors:
            return False
        return all(scales_close(monitor.scale, target) for monitor in self.logical_monitors)

    def scales_by_key(self) -> dict[str, float]:
        return {monitor.key: monitor.scale for monitor in self.logical_monitors}


def merge_restore_scales(
    saved: dict[str, float],
    current: dict[str, float],
    game_scale: float = GAME_SCALE,
) -> dict[str, float]:
    """Update the scales to restore later with what the monitors show now.

    A monitor already at ``game_scale`` keeps its saved value: it is at
    100% because of this app (an earlier switch or the test button), not
    because that is the desktop preference. A monitor with no saved value
    is recorded as it is, which also covers a monitor that is really used
    at 100%.
    """
    merged = dict(saved)
    for key, scale in current.items():
        if key not in merged or not scales_close(scale, game_scale):
            merged[key] = scale
    return merged


def restore_target(monitor: LogicalMonitor, saved: dict[str, float]) -> float | None:
    """The desktop scale for ``monitor``, or None to leave it as it is."""
    if monitor.key in saved:
        return saved[monitor.key]
    if scales_close(monitor.scale, GAME_SCALE):
        return FALLBACK_DESKTOP_SCALE
    # Not at the game scale and not something this app changed.
    return None


def relayout(
    monitors: list[LogicalMonitor],
    targets: list[float],
    layout_mode: int | None,
) -> list[tuple[int, int]]:
    """New positions that keep monitors adjacent after their sizes change.

    Only logical layout mode needs this; in physical mode a scale change
    does not change a monitor's size.
    """
    positions = [(monitor.x, monitor.y) for monitor in monitors]
    if layout_mode != LAYOUT_MODE_LOGICAL or len(monitors) < 2:
        return positions
    old = [monitor.logical_size(monitor.scale, layout_mode) for monitor in monitors]
    new = [monitor.logical_size(target, layout_mode) for monitor, target in zip(monitors, targets)]
    xs = _shift_axis([m.x for m in monitors], [s[0] for s in old], [s[0] for s in new])
    ys = _shift_axis([m.y for m in monitors], [s[1] for s in old], [s[1] for s in new])
    return list(zip(xs, ys))


def _shift_axis(starts: list[int], old_len: list[int], new_len: list[int]) -> list[int]:
    """Move each span that began where an earlier span ended.

    A span that starts right at another span's old end moves to that span's
    new end. Anything else (the left/top edge, or a monitor offset under a
    wider one) keeps its position.
    """
    placed: dict[int, int] = {}
    for index in sorted(range(len(starts)), key=lambda i: starts[i]):
        touching = [j for j in placed if starts[j] + old_len[j] == starts[index]]
        if touching:
            placed[index] = max(placed[j] + new_len[j] for j in touching)
        else:
            placed[index] = starts[index]
    return [placed[index] for index in range(len(starts))]


# Called with True when a scale change was accepted (or not needed).
DoneCallback = Callable[[bool], None]


@dataclass(frozen=True)
class _ApplyRequest:
    parameters: GLib.Variant
    # "125%", or "eDP-1 125%, DP-1 100%", for the log.
    summary: str


class DisplayScaler:
    """Read and set the GNOME display scale."""

    def __init__(self, platform: Platform | None = None) -> None:
        self.platform = platform if platform is not None else current_platform()
        self._proxy: Gio.DBusProxy | None = None
        self._state: DisplayState | None = None
        # Called on the main loop when Mutter reports a monitor change.
        self.on_monitors_changed: Callable[[], None] | None = None
        # Apply failures and read failures are tracked separately so a later
        # successful read does not hide a scale change that Mutter rejected.
        self.apply_error: str | None = None
        self.read_error: str | None = None
        log.info("Platform check: %s", self.platform.describe())

    @property
    def last_error(self) -> str | None:
        return self.apply_error or self.read_error

    def get_state(self, fresh: bool = False) -> DisplayState | None:
        """Current configuration. Cached until Mutter reports a change."""
        refused = self.platform.block_reason()
        if refused:
            # Do not open the Mutter bus on a desktop this app does not change.
            self._remember_read_error(refused)
            return None
        if self._state is not None and not fresh:
            return self._state
        try:
            variant = self._proxy_call("GetCurrentState", None, timeout_ms=3000)
        except GLib.Error as exc:
            self._remember_read_error(f"GetCurrentState failed: {exc.message}")
            return None
        try:
            state = _parse_state(variant.unpack())
        except (ValueError, TypeError, IndexError) as exc:
            self._remember_read_error(f"Could not parse display state: {exc}")
            return None
        self.read_error = None
        self._state = state
        return state

    def get_primary_scale(self) -> float | None:
        state = self.get_state()
        return state.primary_scale if state is not None else None

    def set_scale(self, target: float, on_done: DoneCallback) -> None:
        """Set every logical monitor to ``target``."""
        self.set_scales(lambda _monitor: target, on_done)

    def restore_scales(self, saved: dict[str, float], on_done: DoneCallback) -> None:
        """Put each monitor back to its saved desktop scale."""
        self.set_scales(lambda monitor: restore_target(monitor, saved), on_done)

    def restore_scales_blocking(self, saved: dict[str, float], timeout_ms: int) -> bool:
        """Like :meth:`restore_scales`, but waits. For use while exiting,
        when the main loop will not run a completion callback."""
        request = self._plan(lambda monitor: restore_target(monitor, saved))
        if isinstance(request, bool):
            return request
        try:
            self._proxy_call("ApplyMonitorsConfig", request.parameters, timeout_ms)
        except GLib.Error as exc:
            return self._finish_apply(request, exc)
        return self._finish_apply(request, None)

    def set_scales(
        self,
        choose: Callable[[LogicalMonitor], float | None],
        on_done: DoneCallback,
        timeout_ms: int = 10000,
    ) -> None:
        """Apply ``choose(monitor)`` to each monitor; None keeps its scale.

        Mutter answers ApplyMonitorsConfig only after the modeset, so the
        call runs asynchronously and the window keeps drawing meanwhile.
        ``on_done(ok)`` runs on the main loop: True when Mutter accepted
        the change, or nothing needed to change. When there is nothing to
        send (no change, or an error found before the call), it runs
        before this method returns.
        """
        request = self._plan(choose)
        if isinstance(request, bool):
            on_done(request)
            return

        def finished(error: GLib.Error | None) -> None:
            ok = self._finish_apply(request, error)
            try:
                on_done(ok)
            except Exception:
                log.exception("Scale change callback failed")

        self._proxy_call_async("ApplyMonitorsConfig", request.parameters, timeout_ms, finished)

    def _plan(self, choose: Callable[[LogicalMonitor], float | None]) -> _ApplyRequest | bool:
        """Build the ApplyMonitorsConfig call, or say why none is needed.

        Returns True when every monitor is already at its target, False
        on an error (recorded in ``apply_error`` or ``read_error``).
        """
        refused = self.platform.block_reason()
        if refused:
            self._remember_apply_error(refused)
            return False
        state = self.get_state(fresh=True)
        if state is None:
            return False
        if not state.logical_monitors:
            self._remember_apply_error("No logical monitors reported by Mutter")
            return False

        targets: list[float] = []
        for monitor in state.logical_monitors:
            wanted = choose(monitor)
            if wanted is None:
                targets.append(monitor.scale)
                continue
            snapped = _scale_for_monitor(monitor, wanted)
            if snapped is None:
                log.warning(
                    "%s is not a supported scale for %s (supported: %s). Keeping %s.",
                    format_percent(wanted),
                    monitor.connectors,
                    _advertised(monitor),
                    format_percent(monitor.scale),
                )
                snapped = monitor.scale
            targets.append(snapped)

        summary = _describe(state.logical_monitors, targets)
        if all(scales_close(m.scale, t) for m, t in zip(state.logical_monitors, targets)):
            log.info("Display scale already %s; no change", summary)
            self.apply_error = None
            return True

        positions = relayout(state.logical_monitors, targets, state.layout_mode)
        logical_payload = []
        for monitor, target, (x, y) in zip(state.logical_monitors, targets, positions):
            monitor_entries = [
                (connector, mode_id, {key: GLib.Variant("u", int(value)) for key, value in options.items()})
                for connector, mode_id, options in monitor.monitors
            ]
            logical_payload.append(
                (int(x), int(y), float(target), int(monitor.transform), bool(monitor.primary), monitor_entries)
            )

        properties: dict[str, GLib.Variant] = {}
        if state.supports_layout_mode and state.layout_mode is not None:
            # Keep the current layout mode (logical vs physical). Changing it
            # would resize the desktop independently of the scale factor.
            properties["layout-mode"] = GLib.Variant("u", int(state.layout_mode))
        if state.for_lease:
            properties["monitors-for-lease"] = GLib.Variant("a(ssss)", state.for_lease)

        parameters = GLib.Variant(
            "(uua(iiduba(ssa{sv}))a{sv})",
            (int(state.serial), METHOD_TEMPORARY, logical_payload, properties),
        )
        return _ApplyRequest(parameters, summary)

    def _finish_apply(self, request: _ApplyRequest, error: GLib.Error | None) -> bool:
        # Whatever happened, the cached state is out of date now.
        self._state = None
        if error is not None:
            self._remember_apply_error(f"ApplyMonitorsConfig failed: {error.message or error}")
            return False
        self.apply_error = None
        log.info("Display scale set to %s", request.summary)
        return True

    def _proxy_call(self, method: str, parameters: GLib.Variant | None, timeout_ms: int) -> GLib.Variant:
        return self._get_proxy().call_sync(
            method,
            parameters,
            Gio.DBusCallFlags.NO_AUTO_START,
            timeout_ms,
            None,
        )

    def _proxy_call_async(
        self,
        method: str,
        parameters: GLib.Variant,
        timeout_ms: int,
        callback: Callable[[GLib.Error | None], None],
    ) -> None:
        def finish(proxy: Gio.DBusProxy, result: Gio.AsyncResult, _data) -> None:
            try:
                proxy.call_finish(result)
            except GLib.Error as exc:
                callback(exc)
                return
            callback(None)

        self._get_proxy().call(
            method,
            parameters,
            Gio.DBusCallFlags.NO_AUTO_START,
            timeout_ms,
            None,
            finish,
            None,
        )

    def _get_proxy(self) -> Gio.DBusProxy:
        if self._proxy is None:
            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
                None,
                BUS_NAME,
                OBJECT_PATH,
                INTERFACE,
                None,
            )
            self._proxy.connect("g-signal", self._on_proxy_signal)
        return self._proxy

    def _on_proxy_signal(self, _proxy, _sender, signal_name: str, _parameters) -> None:
        if signal_name != "MonitorsChanged":
            return
        self._state = None
        if self.on_monitors_changed is not None:
            try:
                self.on_monitors_changed()
            except Exception:
                log.exception("Monitor change handler failed")

    def _remember_apply_error(self, message: str) -> None:
        self._log_once(self.apply_error, message)
        self.apply_error = message

    def _remember_read_error(self, message: str) -> None:
        self._log_once(self.read_error, message)
        self.read_error = message

    @staticmethod
    def _log_once(previous: str | None, message: str) -> None:
        # Polling retries the same failure; log it once until the text changes.
        if message != previous:
            log.error("%s", message)


def _describe(monitors: list[LogicalMonitor], targets: list[float]) -> str:
    if len(monitors) == 1:
        return format_percent(targets[0])
    return ", ".join(f"{m.connectors} {format_percent(t)}" for m, t in zip(monitors, targets))


def _advertised(monitor: LogicalMonitor) -> str:
    values = sorted({round(scale, 4) for group in monitor.supported_scales for scale in group})
    return ", ".join(format_percent(value) for value in values) or "unlisted"


def _scale_for_monitor(monitor: LogicalMonitor, target: float) -> float | None:
    """One scale shared by every physical monitor in this logical monitor."""
    groups = [group for group in monitor.supported_scales if group]
    if not groups:
        return float(target)
    snapped = snap_scale(groups[0], target)
    if snapped is None:
        return None
    for group in groups[1:]:
        other = snap_scale(group, snapped)
        if other is None or not scales_close(other, snapped):
            return None
    return snapped


def _mode_props(mode: tuple) -> dict:
    return mode[6] if len(mode) > 6 and isinstance(mode[6], dict) else {}


def _monitor_key(spec: tuple) -> str:
    """Vendor/product/serial survives a dock renaming DP-3 to DP-5."""
    connector, vendor, product, serial = (str(part) for part in spec[:4])
    if vendor or product or serial:
        return f"{vendor}:{product}:{serial}"
    return connector


@dataclass
class _Physical:
    """The parts of a physical monitor a logical monitor needs."""

    mode_id: str
    size: tuple[int, int]
    supported: list[float]
    options: dict[str, int]
    key: str


def _parse_state(unpacked: tuple) -> DisplayState:
    """Turn GetCurrentState's unpacked tuple into a DisplayState.

    The D-Bus signature is::

        (u a((ssss)a(siiddada{sv})a{sv}) a(iiduba(ssss)a{sv}) a{sv})

        serial, physical monitors, logical monitors, properties
    """
    if not isinstance(unpacked, (tuple, list)) or len(unpacked) != 4:
        raise ValueError("unexpected GetCurrentState payload")
    serial, physical, logical, properties = unpacked
    if not isinstance(properties, dict):
        properties = {}

    by_connector: dict[str, _Physical] = {}
    for spec, modes, monitor_props in physical:
        # mode: id, width, height, refresh, preferred scale, supported scales, props
        current = next((mode for mode in modes if _mode_props(mode).get("is-current")), None)
        if current is None:
            current = next((mode for mode in modes if _mode_props(mode).get("is-preferred")), None)
        if current is None:
            continue
        props = monitor_props if isinstance(monitor_props, dict) else {}
        options: dict[str, int] = {}
        # color-mode 0 is "default"; gdctl omits it. rgb-range is 1-based.
        for name in ("color-mode", "rgb-range"):
            if props.get(name):
                options[name] = int(props[name])
        by_connector[spec[0]] = _Physical(
            mode_id=str(current[0]),
            size=(int(current[1]), int(current[2])),
            supported=[float(value) for value in current[5]],
            options=options,
            key=_monitor_key(spec),
        )

    logical_monitors: list[LogicalMonitor] = []
    active_connectors: set[str] = set()
    for x, y, scale, transform, primary, linked, _logical_props in logical:
        members: list[_Physical] = []
        monitors: list[tuple[str, str, dict]] = []
        for linked_spec in linked:
            connector = linked_spec[0]
            active_connectors.add(connector)
            if connector not in by_connector:
                raise ValueError(f"logical monitor references unknown connector {connector}")
            info = by_connector[connector]
            members.append(info)
            monitors.append((connector, info.mode_id, dict(info.options)))
        logical_monitors.append(
            LogicalMonitor(
                x=int(x),
                y=int(y),
                scale=float(scale),
                transform=int(transform),
                primary=bool(primary),
                monitors=monitors,
                supported_scales=[info.supported for info in members],
                mode_size=members[0].size if members else (0, 0),
                key="+".join(sorted(info.key for info in members)),
            )
        )

    for_lease: list[tuple[str, str, str, str]] = []
    for spec, _modes, monitor_props in physical:
        props = monitor_props if isinstance(monitor_props, dict) else {}
        if props.get("is-for-lease") and spec[0] not in active_connectors:
            for_lease.append(tuple(str(part) for part in spec[:4]))  # type: ignore[arg-type]

    layout_mode = properties.get("layout-mode")
    return DisplayState(
        serial=int(serial),
        layout_mode=int(layout_mode) if layout_mode is not None else None,
        supports_layout_mode=bool(properties.get("supports-changing-layout-mode", False)),
        logical_monitors=logical_monitors,
        for_lease=for_lease,
    )
