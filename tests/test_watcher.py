"""Watcher transitions: start/stop delays, per-monitor restore, and holding 100%."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scale_switcher.config import Config, RuntimeState
from scale_switcher.scale import DisplayState, LogicalMonitor, restore_target, scales_close
from scale_switcher.watcher import ProcessView, Watcher


class FakeScaler:
    last_error = None

    def __init__(self, scales):
        self.scales = dict(scales)
        self.on_monitors_changed = None
        self.calls = []
        # When True, completions wait in `waiting` until finish() runs them,
        # like Mutter answering after the modeset.
        self.deferred = False
        self.waiting = []
        self.accept = True

    def _complete(self, apply, on_done):
        def run():
            if self.accept:
                apply()
            on_done(self.accept)

        if self.deferred:
            self.waiting.append(run)
        else:
            run()

    def finish(self):
        waiting, self.waiting = self.waiting, []
        for run in waiting:
            run()

    def get_state(self, fresh=False):
        monitors = [
            LogicalMonitor(x=0, y=0, scale=scale, transform=0, primary=index == 0, key=key)
            for index, (key, scale) in enumerate(self.scales.items())
        ]
        return DisplayState(serial=1, layout_mode=1, supports_layout_mode=True, logical_monitors=monitors)

    def get_primary_scale(self):
        return next(iter(self.scales.values()))

    def set_scale(self, target, on_done):
        self.calls.append(("set", target))

        def apply():
            self.scales = {key: target for key in self.scales}

        self._complete(apply, on_done)

    def _restore(self, saved):
        for monitor in self.get_state().logical_monitors:
            target = restore_target(monitor, saved)
            if target is not None:
                self.scales[monitor.key] = target

    def restore_scales(self, saved, on_done):
        self.calls.append(("restore", dict(saved)))
        self._complete(lambda: self._restore(saved), on_done)

    def restore_scales_blocking(self, saved, timeout_ms):
        self.calls.append(("restore-blocking", dict(saved)))
        self._restore(saved)
        return True


GAME = ProcessView(10, 1, frozenset({"wow.exe"}), "", False, "Wow.exe")


class WatcherTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.config = Config(directory / "config.json")
        self.config.processes = ["Wow.exe"]
        # The round-trip tests below use one 2s delay both ways.
        self.config.start_delay_seconds = 2.0
        self.config.stop_delay_seconds = 2.0
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
        self.assertEqual(self.scaler.calls, [])  # still waiting
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


    def test_short_start_delay_long_stop_delay(self):
        self.config.start_delay_seconds = 0.5
        self.config.stop_delay_seconds = 4.0
        self.running = [GAME]
        self.watcher.poll(now=100)
        self.watcher.poll(now=100.5)
        self.assertTrue(self.watcher.latched_gaming)

        self.running = []
        self.watcher.poll(now=110)
        self.watcher.poll(now=113)
        self.assertTrue(self.watcher.latched_gaming)  # stop delay not over
        self.watcher.poll(now=114)
        self.assertFalse(self.watcher.latched_gaming)

    def test_game_back_within_stop_delay_keeps_100(self):
        # A launcher handing off to the game, or a game restarting itself.
        self.config.start_delay_seconds = 0.5
        self.config.stop_delay_seconds = 4.0
        self.running = [GAME]
        self.watcher.poll(now=100)
        self.watcher.poll(now=101)
        self.running = []
        self.watcher.poll(now=110)
        self.watcher.poll(now=112)
        self.running = [GAME]
        self.watcher.poll(now=113)
        self.running = []
        self.watcher.poll(now=114)
        self.watcher.poll(now=116)
        self.assertTrue(self.watcher.latched_gaming)  # the stop delay started over
        self.assertEqual([call[0] for call in self.scaler.calls], ["set"])

    def test_failed_switch_waits_before_retrying(self):
        self.config.start_delay_seconds = 0.0
        self.scaler.accept = False
        self.running = [GAME]
        with self.assertLogs("scale_switcher", "ERROR"):
            self.watcher.poll(now=100)
        self.watcher.poll(now=101)
        self.watcher.poll(now=104)
        self.assertEqual(len(self.scaler.calls), 1)
        self.watcher.poll(now=105)
        self.assertEqual(len(self.scaler.calls), 2)

    def test_one_change_in_flight_at_a_time(self):
        self.config.start_delay_seconds = 0.0
        self.scaler.deferred = True
        self.running = [GAME]
        self.watcher.poll(now=100)
        self.watcher.poll(now=101)
        self.watcher.test_game_scale()
        self.assertEqual(self.scaler.calls, [("set", 1.0)])
        self.assertFalse(self.watcher.latched_gaming)  # Mutter has not answered

        self.scaler.finish()
        self.assertTrue(self.watcher.latched_gaming)
        self.assertEqual(self.scaler.scales["laptop"], 1.0)

    def test_game_exits_while_100_is_applied(self):
        self.config.start_delay_seconds = 0.0
        self.config.stop_delay_seconds = 1.0
        self.scaler.deferred = True
        self.running = [GAME]
        self.watcher.poll(now=100)
        self.running = []
        self.watcher.poll(now=100.5)
        self.scaler.finish()
        # The display is at 100%, so that is latched; the restore follows.
        self.assertTrue(self.watcher.latched_gaming)
        self.watcher.poll(now=101)
        self.watcher.poll(now=102)
        self.scaler.finish()
        self.assertFalse(self.watcher.latched_gaming)
        self.assertEqual(self.scaler.scales, {"laptop": 1.25, "external": 1.0})

    def test_reapply_waits_for_change_in_flight(self):
        self.config.start_delay_seconds = 0.0
        self.running = [GAME]
        self.watcher.poll(now=100)
        self.assertTrue(self.watcher.latched_gaming)
        # A test-button restore is in flight when the display resets.
        self.scaler.deferred = True
        self.watcher.test_restore()
        self.scaler.scales = {"laptop": 1.25, "external": 1.0}
        self.watcher._drift_since = 100
        self.watcher.poll(now=101)
        self.assertEqual(self.watcher._drift_since, 100)  # still pending
        self.scaler.finish()
        self.scaler.deferred = False
        self.watcher.poll(now=102)
        self.assertEqual(self.scaler.scales["laptop"], 1.0)


class ConfigDelaysTest(unittest.TestCase):
    def test_legacy_debounce_is_replaced(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.json"
            path.write_text('{"processes": ["Wow.exe"], "debounce_seconds": 2.0}')
            config = Config(path)
            self.assertEqual((config.start_delay_seconds, config.stop_delay_seconds), (0.5, 4.0))
            saved = path.read_text()
            self.assertNotIn("debounce_seconds", saved)
            self.assertIn('"stop_delay_seconds": 4.0', saved)

    def test_delays_are_clamped(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.json"
            path.write_text('{"start_delay_seconds": -3, "stop_delay_seconds": 999}')
            config = Config(path)
            self.assertEqual((config.start_delay_seconds, config.stop_delay_seconds), (0.0, 60.0))


if __name__ == "__main__":
    unittest.main()
