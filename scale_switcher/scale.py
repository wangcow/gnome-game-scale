"""Change the GNOME display scale.

Primary path (Wayland and GNOME on X11)
---------------------------------------
Mutter exposes ``org.gnome.Mutter.DisplayConfig`` on the session bus.
``GetCurrentState`` returns every logical monitor; ``ApplyMonitorsConfig``
applies a new scale without changing resolution, position, rotation, or
which monitor is primary.

The apply method used here is *temporary* (method 1). The scale changes
immediately in the running session, and ``~/.config/monitors.xml`` is left
alone, so a later login still restores the user's saved preference
(normally 125%). A persistent apply (method 2) would rewrite that file.

Method 0 only asks Mutter whether the configuration is valid. It does not
change the displays. The test buttons and the watcher use method 1.

X11 fallback
------------
If the DisplayConfig call fails and ``XDG_SESSION_TYPE`` is ``x11``, try
``xrandr --scale``. A UI scale of 125% is a framebuffer scale of 0.8
(1 / 1.25). This is a last resort: GNOME on X11 normally still speaks
DisplayConfig, and ``xrandr`` can fight the desktop. Fractional scale is
not available through ``org.gnome.desktop.interface scaling-factor``
(that key is integer-only), and text-scaling-factor is intentionally not
used because it also applies on Wayland.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from gi.repository import Gio, GLib

log = logging.getLogger("scale_switcher")

# 100% while a monitored app is running, 125% the rest of the time.
GAME_SCALE = 1.0
NORMAL_SCALE = 1.25

# Mutter ApplyMonitorsConfig method argument.
METHOD_VERIFY = 0
METHOD_TEMPORARY = 1
METHOD_PERSISTENT = 2

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
    advertise scales; the caller may pass ``target`` through.
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


class DisplayScaler:
    """Read and set the GNOME display scale."""

    def __init__(self) -> None:
        self._proxy: Gio.DBusProxy | None = None
        # Apply failures and read failures are tracked separately so a later
        # successful read does not hide a scale change that Mutter rejected.
        self.apply_error: str | None = None
        self.read_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self.apply_error or self.read_error

    def get_state(self) -> DisplayState | None:
        try:
            variant = self._proxy_call("GetCurrentState", None, timeout_ms=3000)
        except GLib.Error as exc:
            self._remember_read_error(f"GetCurrentState failed: {exc.message}")
            return None
        try:
            state = _parse_state(variant.unpack())
        except (ValueError, TypeError) as exc:
            self._remember_read_error(f"Could not parse display state: {exc}")
            return None
        self.read_error = None
        return state

    def get_primary_scale(self) -> float | None:
        state = self.get_state()
        if state is None:
            return None
        return state.primary_scale

    def set_scale(self, target: float, method: int = METHOD_TEMPORARY) -> bool:
        """Set every logical monitor to ``target``.

        ``method`` is Mutter's apply method: 0 verify, 1 temporary, 2 persistent.
        The watcher uses temporary. Returns True when Mutter accepts the
        request, including when the scale is already correct.
        """
        state = self.get_state()
        if state is None:
            if method != METHOD_VERIFY and _session_type() == "x11":
                log.warning("Mutter display state is unavailable. Trying xrandr.")
                return self._set_scale_x11(target)
            return False
        if not state.logical_monitors:
            self._remember_apply_error("No logical monitors reported by Mutter")
            return False

        if method != METHOD_VERIFY and state.all_at(target):
            log.info(
                "Display scale already at %s; no change",
                format_percent(target),
            )
            self.apply_error = None
            return True

        logical_payload: list[tuple] = []
        for monitor in state.logical_monitors:
            snapped = _scale_for_monitor(monitor, target)
            if snapped is None:
                advertised = sorted(
                    {round(scale, 4) for group in monitor.supported_scales for scale in group}
                )
                connectors = ", ".join(item[0] for item in monitor.monitors) or "unknown"
                self._remember_apply_error(
                    f"{format_percent(target)} is not a supported scale for {connectors} "
                    f"(supported: {advertised or 'unlisted'})"
                )
                return False
            monitor_entries = []
            for connector, mode_id, options in monitor.monitors:
                variant_options = {
                    key: GLib.Variant("u", int(value))
                    for key, value in options.items()
                }
                monitor_entries.append((connector, mode_id, variant_options))
            logical_payload.append(
                (
                    int(monitor.x),
                    int(monitor.y),
                    float(snapped),
                    int(monitor.transform),
                    bool(monitor.primary),
                    monitor_entries,
                )
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
            (int(state.serial), int(method), logical_payload, properties),
        )
        try:
            self._proxy_call("ApplyMonitorsConfig", parameters, timeout_ms=10000)
        except GLib.Error as exc:
            message = exc.message or str(exc)
            if method != METHOD_VERIFY and _session_type() == "x11":
                log.warning("Mutter rejected the scale change (%s). Trying xrandr.", message)
                if self._set_scale_x11(target):
                    self.apply_error = None
                    return True
            self._remember_apply_error(f"ApplyMonitorsConfig failed: {message}")
            return False

        self.apply_error = None
        if method == METHOD_VERIFY:
            log.info("Mutter accepted a %s scale configuration (verify only)", format_percent(target))
        else:
            how = "temporarily" if method == METHOD_TEMPORARY else "persistently"
            log.info(
                "Display scale set to %s (%s) on %d logical monitor(s)",
                format_percent(target),
                how,
                len(logical_payload),
            )
        return True

    def _set_scale_x11(self, target: float) -> bool:
        """Approximate a scale with xrandr when Mutter is unavailable.

        UI scale 1.25 means the framebuffer is scaled by 1/1.25 = 0.8 so
        the same pixels cover more of the panel. Scale 1.0 clears that.
        """
        xrandr = shutil.which("xrandr")
        if not xrandr:
            self._remember_apply_error("xrandr is not installed; X11 scale fallback is unavailable")
            return False
        try:
            query = subprocess.run(
                [xrandr, "--query"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._remember_apply_error(f"xrandr --query failed: {exc}")
            return False
        if query.returncode != 0:
            self._remember_apply_error(f"xrandr --query failed: {query.stderr.strip()}")
            return False

        outputs = _connected_outputs(query.stdout)
        if not outputs:
            self._remember_apply_error("xrandr reported no connected outputs")
            return False

        factor = 1.0 / target
        scale_arg = f"{factor:.4f}x{factor:.4f}"
        for output in outputs:
            result = subprocess.run(
                [xrandr, "--output", output, "--scale", scale_arg],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                self._remember_apply_error(f"xrandr failed for {output}: {detail}")
                return False
            log.info("xrandr set %s scale to %s (UI %s)", output, scale_arg, format_percent(target))
        log.warning(
            "X11 fallback used xrandr. GNOME may reset it. Fractional scale "
            "via Settings is a Wayland feature."
        )
        self.apply_error = None
        return True

    def _proxy_call(self, method: str, parameters: GLib.Variant | None, timeout_ms: int) -> GLib.Variant:
        proxy = self._get_proxy()
        return proxy.call_sync(
            method,
            parameters,
            Gio.DBusCallFlags.NO_AUTO_START,
            timeout_ms,
            None,
        )

    def _get_proxy(self) -> Gio.DBusProxy:
        if self._proxy is None:
            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                BUS_NAME,
                OBJECT_PATH,
                INTERFACE,
                None,
            )
        return self._proxy

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


def _session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "").strip().lower()


def _connected_outputs(xrandr_query: str) -> list[str]:
    outputs = []
    for line in xrandr_query.splitlines():
        if " connected" not in line:
            continue
        name = line.split()[0]
        if name:
            outputs.append(name)
    return outputs


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


def _parse_state(unpacked: tuple) -> DisplayState:
    """Turn GetCurrentState's unpacked tuple into a DisplayState.

    The D-Bus signature is::

        (u a((ssss)a(siiddada{sv})a{sv}) a(iiduba(ssss)a{sv}) a{sv})

        serial, physical monitors, logical monitors, properties
    """
    if not isinstance(unpacked, (tuple, list)) or len(unpacked) != 4:
        raise ValueError(f"unexpected GetCurrentState payload length {len(unpacked)!r}")
    serial, physical, logical, properties = unpacked
    if not isinstance(properties, dict):
        properties = {}

    # connector -> (current mode id, supported scales, options to preserve)
    by_connector: dict[str, tuple[str, list[float], dict]] = {}
    for entry in physical:
        spec, modes, monitor_props = entry
        connector = spec[0]
        current_mode = None
        supported: list[float] = []
        for mode in modes:
            # mode id, width, height, refresh, preferred scale, supported scales, props
            mode_id = mode[0]
            mode_scales = [float(value) for value in mode[5]]
            mode_props = mode[6] if len(mode) > 6 and isinstance(mode[6], dict) else {}
            if mode_props.get("is-current"):
                current_mode = mode_id
                supported = mode_scales
                break
        if current_mode is None:
            for mode in modes:
                mode_props = mode[6] if len(mode) > 6 and isinstance(mode[6], dict) else {}
                if mode_props.get("is-preferred"):
                    current_mode = mode[0]
                    supported = [float(value) for value in mode[5]]
                    break
        if current_mode is None:
            continue
        props = monitor_props if isinstance(monitor_props, dict) else {}
        options: dict[str, int] = {}
        # color-mode 0 is "default"; gdctl omits it. rgb-range is 1-based.
        color_mode = props.get("color-mode")
        if color_mode:
            options["color-mode"] = int(color_mode)
        rgb_range = props.get("rgb-range")
        if rgb_range:
            options["rgb-range"] = int(rgb_range)
        by_connector[connector] = (str(current_mode), supported, options)

    logical_monitors: list[LogicalMonitor] = []
    active_connectors: set[str] = set()
    for entry in logical:
        x, y, scale, transform, primary, linked, _logical_props = entry
        monitors: list[tuple[str, str, dict]] = []
        supported_groups: list[list[float]] = []
        for linked_spec in linked:
            connector = linked_spec[0]
            active_connectors.add(connector)
            if connector not in by_connector:
                raise ValueError(f"logical monitor references unknown connector {connector}")
            mode_id, supported, options = by_connector[connector]
            monitors.append((connector, mode_id, dict(options)))
            supported_groups.append(supported)
        logical_monitors.append(
            LogicalMonitor(
                x=int(x),
                y=int(y),
                scale=float(scale),
                transform=int(transform),
                primary=bool(primary),
                monitors=monitors,
                supported_scales=supported_groups,
            )
        )

    for_lease: list[tuple[str, str, str, str]] = []
    for entry in physical:
        spec, _modes, monitor_props = entry
        props = monitor_props if isinstance(monitor_props, dict) else {}
        connector = spec[0]
        if props.get("is-for-lease") and connector not in active_connectors:
            for_lease.append((str(spec[0]), str(spec[1]), str(spec[2]), str(spec[3])))

    layout_mode = properties.get("layout-mode")
    return DisplayState(
        serial=int(serial),
        layout_mode=int(layout_mode) if layout_mode is not None else None,
        supports_layout_mode=bool(properties.get("supports-changing-layout-mode", False)),
        logical_monitors=logical_monitors,
        for_lease=for_lease,
    )
