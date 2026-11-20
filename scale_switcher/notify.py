"""Desktop notifications.

``notify-send`` is preferred. It works for a background process that was
started by systemd before a desktop file is installed. It runs without
blocking the main loop, and each notification replaces the previous one
so scale changes do not pile up. If it is missing or fails, fall back to
``Gio.Notification`` on the GTK application.
"""

from __future__ import annotations

import logging
import shutil

from gi.repository import Gio, GLib

from scale_switcher.brand import app_icon_path

log = logging.getLogger("scale_switcher")


class Notifier:
    def __init__(self, application: Gio.Application) -> None:
        self._application = application
        # notify-send's id for the last notification, for --replace-id.
        self._last_id: str | None = None

    def send(self, title: str, body: str, enabled: bool) -> None:
        if not enabled:
            log.info("Notification off: %s — %s", title, body)
            return
        log.info("Notification: %s — %s", title, body)
        if not self._notify_send(title, body):
            self._gio(title, body)

    def _notify_send(self, title: str, body: str) -> bool:
        executable = shutil.which("notify-send")
        if not executable:
            return False
        argv = [executable, "--app-name=Scale Switcher", "--print-id"]
        if self._last_id:
            argv.append(f"--replace-id={self._last_id}")
        icon = app_icon_path(48)
        if icon.is_file():
            argv.append(f"--icon={icon}")
        argv += [title, body]
        try:
            process = Gio.Subprocess.new(
                argv,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
        except GLib.Error as exc:
            log.warning("notify-send failed to start: %s", exc.message)
            return False
        process.communicate_utf8_async(None, None, self._on_notify_send_done, (title, body))
        return True

    def _on_notify_send_done(self, process: Gio.Subprocess, result, message: tuple[str, str]) -> None:
        try:
            _ok, stdout, stderr = process.communicate_utf8_finish(result)
        except GLib.Error as exc:
            log.warning("notify-send failed: %s", exc.message)
            self._gio(*message)
            return
        if not process.get_successful():
            log.warning("notify-send exited %s: %s", process.get_exit_status(), (stderr or "").strip())
            # An old notify-send without --print-id lands here once; the
            # Gio path still shows the message.
            self._gio(*message)
            return
        self._last_id = (stdout or "").strip() or None

    def _gio(self, title: str, body: str) -> None:
        try:
            notification = Gio.Notification.new(title)
            notification.set_body(body)
            icon = app_icon_path(48)
            if icon.is_file():
                notification.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(str(icon))))
            # The same id replaces the previous notification.
            self._application.send_notification("scale-switcher", notification)
        except GLib.Error as exc:
            log.warning("Gio.Notification failed: %s", exc.message)
