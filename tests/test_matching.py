"""Process matching, including Wine command lines and the Lutris rules."""

from __future__ import annotations

import os
import unittest

from scale_switcher.config import _normalize_process_name
from scale_switcher.watcher import (
    LaunchTracker,
    ProcessView,
    candidate_app_names,
    exe_names,
    find_matches,
    format_labels,
)


def view(pid, names, ppid=1, label=None, lutris_game=False, cmdline=""):
    names = frozenset(name.casefold() for name in names)
    return ProcessView(pid, ppid, names, cmdline, lutris_game, label or sorted(names)[0], uid=os.getuid())


class ExeNamesTest(unittest.TestCase):
    def test_windows_path_with_spaces_in_directories(self):
        self.assertEqual(
            exe_names(r"C:\Program Files (x86)\Battle.net\Battle.net.exe --in-process-gpu"),
            ["Battle.net.exe"],
        )

    def test_windows_file_name_with_spaces(self):
        self.assertEqual(
            exe_names(r"C:\Games\World of Warcraft Launcher.exe"),
            ["World of Warcraft Launcher.exe"],
        )

    def test_unix_wine_invocation(self):
        self.assertEqual(exe_names("/usr/bin/wine start.exe /unix /home/me/game.exe"), ["start.exe", "game.exe"])

    def test_not_an_exe(self):
        self.assertEqual(exe_names(r"C:\logs\run.exe.log --x=foo.exed"), [])

    def test_quoted(self):
        self.assertEqual(exe_names('"Z:\\games\\Wow.exe" -launcherlogin'), ["Wow.exe"])


class NormalizeTest(unittest.TestCase):
    def test_windows_path_keeps_file_name(self):
        self.assertEqual(_normalize_process_name(r"C:\windows\system32\explorer.exe"), "explorer.exe")

    def test_unix_path(self):
        self.assertEqual(_normalize_process_name("/usr/bin/steam"), "steam")

    def test_rejects_blank(self):
        self.assertEqual(_normalize_process_name("   "), "")


class MatchTest(unittest.TestCase):
    def test_name_match_is_case_insensitive(self):
        game = view(10, {"Battle.net.exe", "wine64-preloader"}, label="Battle.net.exe")
        self.assertEqual([m.pid for m in find_matches([game], ["battle.net.exe"])], [10])

    def test_lutris_client_alone_does_not_match(self):
        lutris = view(1, {"lutris", "python3"})
        self.assertEqual(find_matches([lutris], ["lutris"]), [])

    def test_lutris_child_game_matches(self):
        lutris = view(1, {"lutris", "python3"})
        game = view(2, {"wow.exe", "wine64-preloader"}, ppid=1)
        self.assertEqual([m.reason for m in find_matches([lutris, game], ["lutris"])], ["lutris-child"])

    def test_leftover_wine_services_do_not_hold_game_mode(self):
        # Under Wine the exe is the preloader, so names carry both.
        lutris = view(1, {"lutris", "python3"})
        explorer = view(2, {"explorer.exe", "wine64-preloader"}, ppid=1, lutris_game=True)
        server = view(3, {"wineserver"}, ppid=1, lutris_game=True)
        self.assertEqual(find_matches([lutris, explorer, server], ["lutris"], {2, 3}), [])

    def test_explicit_wine_service_name_still_matches(self):
        explorer = view(2, {"explorer.exe", "wine64-preloader"})
        self.assertEqual(len(find_matches([explorer], ["explorer.exe"])), 1)

    def test_game_with_uuid_outside_tree(self):
        lutris = view(1, {"lutris"})
        game = view(50, {"game.exe", "wine64-preloader"}, ppid=999, lutris_game=True)
        self.assertEqual([m.reason for m in find_matches([lutris, game], ["lutris"])], ["lutris-uuid"])


class LaunchTrackerTest(unittest.TestCase):
    def test_only_processes_after_baseline(self):
        tracker = LaunchTracker()
        old = view(5, {"old.exe"})
        self.assertEqual(tracker.game_pids([old], True), set())
        new = view(6, {"new.exe"})
        self.assertEqual(tracker.game_pids([old, new], True), {6})
        self.assertEqual(tracker.game_pids([old, new], False), set())


class PickerTest(unittest.TestCase):
    def test_hides_session_noise_and_wine_services(self):
        processes = [
            view(1, {"gsd-power"}),
            view(2, {"explorer.exe", "wine64-preloader"}, label="explorer.exe"),
            view(3, {"wow.exe", "wine64-preloader"}, label="Wow.exe"),
            view(4, {"bash"}),
            view(5, {"steam"}, label="steam"),
            ProcessView(6, 2, frozenset({"abrtd"}), "", False, "abrtd", uid=0),
        ]
        self.assertEqual(candidate_app_names(processes), ["steam", "Wow.exe"])


class FormatLabelsTest(unittest.TestCase):
    def test_unique_and_limited(self):
        self.assertEqual(format_labels(["a", "a", "b", "c", "d"]), "a, b, c (+1 more)")
        self.assertEqual(format_labels([]), "unknown")


if __name__ == "__main__":
    unittest.main()
