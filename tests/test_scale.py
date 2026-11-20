"""Mutter state parsing, per-monitor restore, and relayout."""

from __future__ import annotations

import unittest

from scale_switcher.platform import detect_platform
from scale_switcher.scale import (
    FALLBACK_DESKTOP_SCALE,
    DisplayScaler,
    _parse_state,
    merge_restore_scales,
    relayout,
    restore_target,
    snap_scale,
)

GNOME = detect_platform(
    {"XDG_CURRENT_DESKTOP": "GNOME", "XDG_SESSION_TYPE": "wayland"},
    'ID=fedora\nPRETTY_NAME="Fedora Linux 44"\n',
)

LAPTOP_SCALES = [1.0, 1.25, 1.5, 1.75, 2.0]
EXTERNAL_SCALES = [1.0, 1.25, 1.5]


def physical(connector, vendor, width, height, scales):
    mode = ("mode-" + connector, width, height, 60.0, 1.0, scales, {"is-current": True})
    return ((connector, vendor, "panel", "0001"), [mode], {})


def logical(x, y, scale, primary, connector):
    return (x, y, scale, 0, primary, [(connector, "", "", "")], {})


def payload(logicals, layout_mode=1):
    physicals = [
        physical("eDP-1", "BOE", 1920, 1080, LAPTOP_SCALES),
        physical("DP-1", "DEL", 2560, 1440, EXTERNAL_SCALES),
    ]
    return (7, physicals, logicals, {"layout-mode": layout_mode, "supports-changing-layout-mode": True})


class ParseTest(unittest.TestCase):
    def test_keys_sizes_and_scales(self):
        state = _parse_state(payload([logical(0, 0, 1.25, True, "eDP-1"), logical(1536, 0, 1.0, False, "DP-1")]))
        laptop, external = state.logical_monitors
        self.assertEqual(laptop.key, "BOE:panel:0001")
        self.assertEqual(laptop.mode_size, (1920, 1080))
        self.assertEqual(state.scales_by_key(), {"BOE:panel:0001": 1.25, "DEL:panel:0001": 1.0})
        self.assertEqual(state.primary_scale, 1.25)


class RelayoutTest(unittest.TestCase):
    def test_side_by_side_moves_the_right_monitor(self):
        state = _parse_state(payload([logical(0, 0, 1.25, True, "eDP-1"), logical(1536, 0, 1.0, False, "DP-1")]))
        positions = relayout(state.logical_monitors, [1.0, 1.0], state.layout_mode)
        self.assertEqual(positions, [(0, 0), (1920, 0)])

    def test_round_trip(self):
        state = _parse_state(payload([logical(0, 0, 1.0, True, "eDP-1"), logical(1920, 0, 1.0, False, "DP-1")]))
        self.assertEqual(relayout(state.logical_monitors, [1.25, 1.0], 1), [(0, 0), (1536, 0)])

    def test_stacked_moves_the_lower_monitor(self):
        state = _parse_state(payload([logical(0, 0, 1.0, False, "DP-1"), logical(320, 1440, 1.25, True, "eDP-1")]))
        self.assertEqual(relayout(state.logical_monitors, [1.0, 1.0], 1), [(0, 0), (320, 1440)])
        state = _parse_state(payload([logical(0, 0, 1.25, True, "eDP-1"), logical(0, 864, 1.0, False, "DP-1")]))
        self.assertEqual(relayout(state.logical_monitors, [1.0, 1.0], 1), [(0, 0), (0, 1080)])

    def test_physical_layout_mode_keeps_positions(self):
        state = _parse_state(payload([logical(0, 0, 1.25, True, "eDP-1"), logical(1920, 0, 1.0, False, "DP-1")], 2))
        self.assertEqual(relayout(state.logical_monitors, [1.0, 1.0], 2), [(0, 0), (1920, 0)])


class RestoreTest(unittest.TestCase):
    def test_merge_keeps_saved_value_for_monitor_at_game_scale(self):
        merged = merge_restore_scales({"laptop": 1.25}, {"laptop": 1.0, "external": 1.0})
        self.assertEqual(merged, {"laptop": 1.25, "external": 1.0})

    def test_merge_takes_a_changed_desktop_scale(self):
        self.assertEqual(merge_restore_scales({"laptop": 1.25}, {"laptop": 1.5}), {"laptop": 1.5})

    def test_restore_target(self):
        state = _parse_state(payload([logical(0, 0, 1.0, True, "eDP-1"), logical(1920, 0, 1.5, False, "DP-1")]))
        laptop, external = state.logical_monitors
        self.assertEqual(restore_target(laptop, {"BOE:panel:0001": 1.75}), 1.75)
        # No saved value: 100% falls back, anything else is left alone.
        self.assertEqual(restore_target(laptop, {}), FALLBACK_DESKTOP_SCALE)
        self.assertIsNone(restore_target(external, {}))

    def test_snap(self):
        self.assertEqual(snap_scale([1.0, 1.2500000476837158], 1.25), 1.2500000476837158)
        self.assertIsNone(snap_scale([1.0, 1.5], 1.25))


class FakeMutterScaler(DisplayScaler):
    """DisplayScaler with the D-Bus calls replaced."""

    def __init__(self, logicals):
        super().__init__(platform=GNOME)
        self.logicals = logicals
        self.applied = None

    def _proxy_call(self, method, parameters, timeout_ms):
        if method == "GetCurrentState":
            class _Variant:
                def unpack(inner):
                    return payload(self.logicals)
            return _Variant()
        self.applied = parameters.unpack()
        return None

    def _proxy_call_async(self, method, parameters, timeout_ms, callback):
        self.applied = parameters.unpack()
        self.pending = callback


def run(start):
    """Call an async scaler method; return what on_done received."""
    results = []
    start(results.append)
    return results


class ApplyTest(unittest.TestCase):
    def test_restore_payload(self):
        scaler = FakeMutterScaler([logical(0, 0, 1.0, True, "eDP-1"), logical(1920, 0, 1.0, False, "DP-1")])
        results = run(lambda done: scaler.restore_scales({"BOE:panel:0001": 1.25, "DEL:panel:0001": 1.0}, done))
        self.assertEqual(results, [])  # waiting for Mutter
        scaler.pending(None)
        self.assertEqual(results, [True])
        serial, method, monitors, properties = scaler.applied
        self.assertEqual((serial, method), (7, 1))
        self.assertEqual([(m[0], m[1], m[2]) for m in monitors], [(0, 0, 1.25), (1536, 0, 1.0)])
        self.assertEqual(monitors[0][5], [("eDP-1", "mode-eDP-1", {})])
        self.assertEqual(properties, {"layout-mode": 1})

    def test_no_change_needed(self):
        scaler = FakeMutterScaler([logical(0, 0, 1.0, True, "eDP-1")])
        self.assertEqual(run(lambda done: scaler.set_scale(1.0, done)), [True])
        self.assertIsNone(scaler.applied)

    def test_unsupported_scale_keeps_current(self):
        scaler = FakeMutterScaler([logical(0, 0, 1.0, True, "eDP-1"), logical(1920, 0, 1.0, False, "DP-1")])
        with self.assertLogs("scale_switcher", "WARNING"):
            run(lambda done: scaler.restore_scales({"BOE:panel:0001": 1.25, "DEL:panel:0001": 1.75}, done))
        self.assertEqual([m[2] for m in scaler.applied[2]], [1.25, 1.0])

    def test_rejected_by_mutter(self):
        from gi.repository import GLib

        scaler = FakeMutterScaler([logical(0, 0, 1.25, True, "eDP-1")])
        results = run(lambda done: scaler.set_scale(1.0, done))
        with self.assertLogs("scale_switcher", "ERROR"):
            scaler.pending(GLib.Error("Logical monitors not adjacent"))
        self.assertEqual(results, [False])
        self.assertIn("not adjacent", scaler.apply_error)

    def test_blocking_restore_for_exit(self):
        scaler = FakeMutterScaler([logical(0, 0, 1.0, True, "eDP-1")])
        self.assertTrue(scaler.restore_scales_blocking({"BOE:panel:0001": 1.25}, 3000))
        self.assertEqual(scaler.applied[2][0][2], 1.25)


if __name__ == "__main__":
    unittest.main()
