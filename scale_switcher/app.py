"""libadwaita window for Scale Switcher.

Closing the window hides it and leaves the watcher running, so a systemd
user service (or a second launch) can keep monitoring games. The menu
Quit action stops the process. A second launch of the same application id
presents this window again.
"""

from __future__ import annotations

import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from scale_switcher.config import Config, RuntimeState  # noqa: E402
from scale_switcher.notify import Notifier  # noqa: E402
from scale_switcher.scale import (  # noqa: E402
    GAME_SCALE,
    NORMAL_SCALE,
    DisplayScaler,
    format_percent,
)
from scale_switcher.watcher import Watcher, list_running_app_names  # noqa: E402

log = logging.getLogger("scale_switcher")

APP_ID = "io.github.scaleswitcher.ScaleSwitcher"


class ScaleSwitcherApp(Adw.Application):
    def __init__(self, background: bool = False) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self._background_first = background
        self.config = Config()
        self.runtime_state = RuntimeState()
        self.scaler = DisplayScaler()
        self.notifier = Notifier(self)
        self.watcher = Watcher(
            config=self.config,
            state=self.runtime_state,
            scaler=self.scaler,
            notify=self.notifier.send,
            on_status=self._on_status,
        )
        self.window: Adw.ApplicationWindow | None = None
        self.status_row: Adw.ActionRow | None = None
        self._process_rows: list[Gtk.Widget] = []
        self._proc_group: Adw.PreferencesGroup | None = None
        self._entry: Adw.EntryRow | None = None
        self._add_dialog: _AddAppDialog | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # Keep running after the window is hidden.
        self.hold()
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", self._on_quit)
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<primary>q"])

    def do_activate(self) -> None:
        if self.window is None:
            self.window = self._build_window()
            self.watcher.start()
        # --background hides the first presentation (login service).
        # Any later activation, including a second process, shows the window.
        if self._background_first:
            self._background_first = False
            log.info("Started in the background. Open Scale Switcher again to show the window.")
            return
        self.window.present()

    def do_shutdown(self) -> None:
        self.watcher.stop()
        log.info("Scale Switcher stopped")
        Adw.Application.do_shutdown(self)

    def _build_window(self) -> Adw.ApplicationWindow:
        window = Adw.ApplicationWindow(application=self)
        window.set_title("Scale Switcher")
        window.set_default_size(480, 640)
        window.connect("close-request", self._on_close)

        header = Adw.HeaderBar()
        title = Adw.WindowTitle.new("Scale Switcher", "100% in games, 125% on the desktop")
        header.set_title_widget(title)
        menu = Gio.Menu()
        menu.append("Quit", "app.quit")
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(menu)
        menu_button.set_tooltip_text("Menu")
        header.pack_end(menu_button)

        page = Adw.PreferencesPage()
        page.add(self._build_watcher_group())
        page.add(self._build_process_group())
        page.add(self._build_test_group())

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        window.set_content(toolbar)
        return window

    def _build_watcher_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Watcher",
            description=(
                "Closing the window keeps the watcher running. "
                "Quit from the menu to stop it."
            ),
        )
        enable = Adw.SwitchRow(
            title="Enable watcher",
            subtitle="Switch to 100% while a monitored app is running, then back to 125%.",
            active=self.config.enabled,
        )
        enable.connect("notify::active", self._on_enable)
        group.add(enable)

        notifications = Adw.SwitchRow(
            title="Notifications",
            subtitle="Show a desktop notification when the scale changes.",
            active=self.config.notifications,
        )
        notifications.connect("notify::active", self._on_notifications)
        group.add(notifications)

        self.status_row = Adw.ActionRow(title="Display scale", subtitle="Reading…")
        icon = Gtk.Image.new_from_icon_name("video-display-symbolic")
        self.status_row.add_prefix(icon)
        group.add(self.status_row)
        return group

    def _build_process_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Monitored processes",
            description=(
                "Start the app and pick its name, or type a process name. "
                '"lutris" watches games Lutris launches, not the Lutris window.'
            ),
        )
        self._proc_group = group
        add_button = Adw.ActionRow(title="Add a running app")
        add_button.set_subtitle("Start the app, then choose the name that appears.")
        add_button.set_activatable(True)
        add_button.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        add_button.connect("activated", self._on_add_running)
        group.add(add_button)
        entry = Adw.EntryRow(title="Process name")
        entry.set_show_apply_button(True)
        entry.set_tooltip_text("Example: steam, heroic, lutris, Cyberpunk2077.exe")
        entry.connect("apply", self._on_add)
        entry.connect("entry-activated", self._on_add)
        self._entry = entry
        group.add(entry)
        self._rebuild_process_rows()
        return group

    def _build_test_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Test",
            description=(
                "Changes the scale immediately. While the watcher is enabled "
                "it may switch again once the game list settles."
            ),
        )
        to_game = Adw.ButtonRow(title=f"Test switch to {format_percent(GAME_SCALE)}")
        to_game.connect("activated", lambda *_args: self._on_test(GAME_SCALE))
        group.add(to_game)
        to_desktop = Adw.ButtonRow(title=f"Restore {format_percent(NORMAL_SCALE)}")
        to_desktop.connect("activated", lambda *_args: self._on_test(NORMAL_SCALE))
        group.add(to_desktop)
        return group

    def _rebuild_process_rows(self) -> None:
        assert self._proc_group is not None
        for row in self._process_rows:
            self._proc_group.remove(row)
        self._process_rows.clear()
        if not self.config.processes:
            empty = Adw.ActionRow(
                title="No processes yet",
                subtitle="Add a running app, or type a name.",
            )
            self._proc_group.add(empty)
            self._process_rows.append(empty)
            return
        for name in self.config.processes:
            row = Adw.ActionRow(title=name)
            if name.casefold() == "lutris":
                row.set_subtitle("Games launched by Lutris, not the Lutris app")
            button = Gtk.Button(icon_name="list-remove-symbolic")
            button.add_css_class("flat")
            button.set_valign(Gtk.Align.CENTER)
            button.set_tooltip_text(f"Stop monitoring {name}")
            button.connect("clicked", self._on_remove, name)
            row.add_suffix(button)
            self._proc_group.add(row)
            self._process_rows.append(row)

    def _on_enable(self, row: Adw.SwitchRow, _param) -> None:
        self.config.enabled = row.get_active()
        self.config.save()
        log.info("Watcher enabled=%s", self.config.enabled)
        self.watcher.poll()

    def _on_notifications(self, row: Adw.SwitchRow, _param) -> None:
        self.config.notifications = row.get_active()
        self.config.save()
        log.info("Notifications enabled=%s", self.config.notifications)

    def _on_add_running(self, *_args) -> None:
        if self.window is None:
            return
        if self._add_dialog is not None:
            self._add_dialog.present(self.window)
            return
        self._add_dialog = _AddAppDialog(self)
        self._add_dialog.connect("closed", self._on_add_dialog_closed)
        self._add_dialog.present(self.window)

    def _on_add_dialog_closed(self, _dialog) -> None:
        self._add_dialog = None

    def _on_add(self, entry: Adw.EntryRow) -> None:
        if self._add_name(entry.get_text()):
            entry.set_text("")

    def _add_name(self, text: str) -> bool:
        added = self.config.add_process(text)
        if added:
            self._rebuild_process_rows()
            self.watcher.poll()
            return True
        if text.strip():
            log.info("Process name %r was not added (empty, invalid, or already listed)", text.strip())
        return False

    def _on_remove(self, _button, name: str) -> None:
        self.config.remove_process(name)
        self._rebuild_process_rows()
        self.watcher.poll()

    def _on_test(self, target: float) -> None:
        self.watcher.test_scale(target)

    def _on_status(self, status: dict) -> None:
        if self.status_row is None:
            return
        scale = status.get("scale")
        scale_text = format_percent(scale) if isinstance(scale, (int, float)) else "unknown"
        if not status.get("enabled"):
            activity = "watcher off"
        elif status.get("pending"):
            target = format_percent(GAME_SCALE) if status.get("pending_gaming") else format_percent(NORMAL_SCALE)
            activity = f"switching to {target}…"
        else:
            labels = _unique_labels(status.get("matches") or [])
            activity = _format_activity(labels) if labels else "idle"
        subtitle = f"{scale_text} · {activity}"
        error = status.get("error")
        if error:
            subtitle = f"{subtitle} — {error}"
        self.status_row.set_subtitle(subtitle)

    def _on_close(self, window: Adw.ApplicationWindow) -> bool:
        window.set_visible(False)
        log.info("Window hidden. The watcher is still running. Use Quit to stop it.")
        return True

    def _on_quit(self, *_args) -> None:
        self.quit()


class _AddAppDialog(Adw.Dialog):
    """Start an app, then pick the process name that shows up.

    Names already running when the dialog opens stay hidden until
    "Show apps already open" is turned on.
    """

    def __init__(self, owner: ScaleSwitcherApp) -> None:
        super().__init__(title="Add an app", content_width=420, content_height=520)
        self._owner = owner
        self._timeout_id = 0
        self._shown: tuple[str, ...] = ()
        self._baseline = {name.casefold() for name in list_running_app_names()}

        self._hint = Gtk.Label(
            label="Start the app. Choose the name that appears.",
            xalign=0,
            wrap=True,
        )
        self._hint.add_css_class("dim-label")
        self._hint.set_margin_start(12)
        self._hint.set_margin_end(12)
        self._hint.set_margin_top(12)
        self._hint.set_margin_bottom(6)

        self._list = Gtk.ListBox()
        self._list.add_css_class("boxed-list")
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled = Gtk.ScrolledWindow(vexpand=True, child=self._list)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_margin_start(12)
        scrolled.set_margin_end(12)
        scrolled.set_margin_bottom(12)

        self._existing = Gtk.ToggleButton(label="Show apps already open")
        self._existing.connect("toggled", self._on_toggle_existing)
        self._existing.set_margin_start(12)
        self._existing.set_margin_end(12)
        self._existing.set_margin_bottom(12)
        self._existing.set_halign(Gtk.Align.START)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self._hint)
        content.append(scrolled)
        content.append(self._existing)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(content)
        self.set_child(toolbar)

        self.connect("closed", self._on_closed)
        self._timeout_id = GLib.timeout_add(1000, self._refresh)
        self._refresh()

    def _on_toggle_existing(self, _button) -> None:
        if self._existing.get_active():
            self._hint.set_text("Choose the app's process name.")
        else:
            self._hint.set_text("Start the app. Choose the name that appears.")
        self._shown = ()
        self._refresh()

    def _refresh(self) -> bool:
        monitored = {name.casefold() for name in self._owner.config.processes}
        visible = []
        for name in list_running_app_names():
            key = name.casefold()
            if key in monitored:
                continue
            if not self._existing.get_active() and key in self._baseline:
                continue
            visible.append(name)
        signature = tuple(visible)
        if signature == self._shown:
            return True
        self._shown = signature
        self._fill(visible)
        return True

    def _fill(self, names: list[str]) -> None:
        child = self._list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list.remove(child)
            child = nxt
        if not names:
            title = "No other apps found." if self._existing.get_active() else "Waiting for a new app…"
            placeholder = Adw.ActionRow(title=title)
            placeholder.set_activatable(False)
            self._list.append(placeholder)
            return
        for name in names:
            row = Adw.ActionRow(title=name)
            row.set_activatable(True)
            if name.casefold() == "lutris":
                row.set_subtitle("Watches games Lutris launches, not the Lutris window")
            row.connect("activated", self._on_pick)
            self._list.append(row)

    def _on_pick(self, row: Adw.ActionRow) -> None:
        if self._owner._add_name(row.get_title() or ""):
            self.close()

    def _on_closed(self, _dialog) -> None:
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0


def _unique_labels(labels: list[str]) -> list[str]:
    unique: list[str] = []
    for label in labels:
        if label not in unique:
            unique.append(label)
    return unique


def _format_activity(labels: list[str]) -> str:
    shown = ", ".join(labels[:3])
    if len(labels) > 3:
        shown += f" (+{len(labels) - 3} more)"
    return shown


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = list(sys.argv if argv is None else argv)
    background = "--background" in args
    args = [arg for arg in args if arg != "--background"]
    log.info("Scale Switcher starting (background=%s)", background)
    app = ScaleSwitcherApp(background=background)
    try:
        return app.run(args)
    except KeyboardInterrupt:
        # Gio raises this after quit() has already run, as its SIGINT fallback.
        # Shutdown logged "Watcher stopped" before this point.
        return 0
    except Exception:
        log.exception("Scale Switcher exited because of an error")
        return 1
