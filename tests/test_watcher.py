"""Watcher transitions: debounce, per-monitor restore, and holding 100%."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scale_switcher.config import Config, RuntimeState
from scale_switcher.scale import DisplayState, LogicalMonitor, restore_target, scales_close
from scale_switcher.watcher import ProcessView, Watcher

from tests.test_scale import GNOME


class FakeScaler:
    platform = GNOME
    last_error = None

    def __init__(self, scales):
        self.scales = dict(scales)
        self.on_monitors_changed = None
        self.calls = []

    def get_state(self, fresh=False):
        monitors = [
            LogicalMonitor(x=0, y=0, scale=scale, transform=0, primary=index == 0, key=key)
            for index, (key, scale) in enumerate(self.scales.items())
        ]
        return DisplayState(serial=1, layout_mode=1, supports_layout_mode=True, logical_monitors=monitors)

    def get_primary_scale(self):
        return next(iter(self.scales.values()))

    def set_scale(self, target):
        self.calls.append(("set", target))
        self.scales = {key: target for key in self.scales}
        return True

    def restore_scales(self, saved, timeout_ms=10000):
        self.calls.append(("restore", dict(saved)))
        for monitor in self.get_state().logical_monitors:
            target = restore_target(monitor, saved)
            if target is not None:
                self.scales[monitor.key] = target
        return True


GAME = ProcessView(10, 1, frozenset({"wow.exe"}), "", False, "Wow.exe")


class WatcherTest(unittest.TestCase):
    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.config = Config(directory / "config.json")
        self.config.processes = ["Wow.exe"]
        self.config.debounce_seconds = 2.0
        self.state = RuntimeState(directory / "state.json")
        self.scaler = FakeScaler({"laptop": 1.25, "external": 1.0})
        self.watcher = Watcher(self.config, self.state, self.scaler, self._notify, lambda status: None)
        self.notes = []
        self.running = []
        patcher = mock.patch("scale_switcher.watcher.take_snapshot", lambda _names: list(self.running))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _notify(self, title, body, enabled):
        self.notes.append(title)

    def test_game_round_trip_restores_each_monitor(self):
        self.running = [GAME]
        self.watcher.poll(now=100)
        self.assertEqual(self.scaler.calls, [])  # still debouncing
        self.watcher.poll(now=102)
        self.assertEqual(self.scaler.scales, {"laptop": 1.0, "external": 1.0})
        self.assertEqual(self.state.restore_scales, {"laptop": 1.25, "external": 1.0})
        self.assertTrue(RuntimeState(self.state.path).gaming_mode)

        self.running = []
        self.watcher.poll(now=110)
        self.watcher.poll(now=112)
        self.assertEqual(self.scaler.scales, {"laptop": 1.25, "external": 1.0})
        self.assertEqual(self.state.restore_scales, {})
        self.assertFalse(self.watcher.latched_gaming)

    def test_reapplies_100_after_monitor_change(self):
        self.running = [GAME]
        self.watcher.poll(now=100)
        self.watcher.poll(now=102)
        # Resume from suspend: Mutter reloads monitors.xml.
        self.scaler.scales = {"laptop": 1.25, "external": 1.0}
        self.watcher._drift_since = 103
        self.watcher.poll(now=104)
        self.assertTrue(scales_close(self.scaler.scales["laptop"], 1.25))  # waiting
        self.watcher.poll(now=105)
        self.assertEqual(self.scaler.scales["laptop"], 1.0)
        # The saved desktop scale survived the second switch.
        self.assertEqual(self.state.restore_scales["laptop"], 1.25)

    def test_no_reapply_when_idle(self):
        self.watcher.poll(now=100)
        self.scaler.scales = {"laptop": 1.5, "external": 1.0}
        self.watcher._drift_since = 100
        self.watcher.poll(now=200)
        self.assertEqual(self.scaler.calls, [])

    def test_crash_recovery_restores_saved_scales(self):
        self.state.set_gaming_mode(True)
        self.state.set_restore_scales({"laptop": 1.25, "external": 1.0})
        self.scaler.scales = {"laptop": 1.0, "external": 1.0}
        watcher = Watcher(self.config, RuntimeState(self.state.path), self.scaler, self._notify, lambda s: None)
        watcher.poll(now=100)
        watcher.poll(now=102)
        self.assertEqual(self.scaler.scales, {"laptop": 1.25, "external": 1.0})

    def test_old_state_file_without_saved_scales(self):
        # Written by the previous version: only the latch, no scales.
        self.state.path.write_text('{"gaming_mode": true}')
        self.scaler.scales = {"laptop": 1.0}
        watcher = Watcher(self.config, RuntimeState(self.state.path), self.scaler, self._notify, lambda s: None)
        watcher.poll(now=100)
        watcher.poll(now=102)
        self.assertEqual(self.scaler.scales, {"laptop": 1.25})

    def test_restore_on_exit(self):
        self.running = [GAME]
        self.watcher.poll(now=100)
        self.watcher.poll(now=102)
        self.watcher.restore_on_exit()
        self.assertEqual(self.scaler.scales["laptop"], 1.25)
        self.assertFalse(RuntimeState(self.state.path).gaming_mode)

    def test_test_buttons(self):
        self.watcher.test_game_scale()
        self.assertEqual(self.scaler.scales["laptop"], 1.0)
        # A second press must not record 100% as the desktop scale.
        self.watcher.test_game_scale()
        self.watcher.test_restore()
        self.assertEqual(self.scaler.scales["laptop"], 1.25)


if __name__ == "__main__":
    unittest.main()
