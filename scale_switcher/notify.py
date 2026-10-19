"""Desktop notifications.

``notify-send`` is preferred. It works for a background process that was
started by systemd before a desktop file is installed. If it is missing
or fails to spawn, fall back to ``Gio.Notification`` on the GTK application.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from gi.repository import Gio, GLib

log = logging.getLogger("scale_switcher")


class Notifier:
    def __init__(self, application) -> None:
        self._application = application

    def send(self, title: str, body: str, enabled: bool) -> None:
        if not enabled:
            log.info("Notification off: %s — %s", title, body)
            return
        if self._notify_send(title, body):
            log.info("Notification: %s — %s", title, body)
            return
        if self._gio(title, body):
            log.info("Notification (Gio): %s — %s", title, body)
            return
        log.warning("Could not show notification: %s — %s", title, body)

    def _notify_send(self, title: str, body: str) -> bool:
        executable = shutil.which("notify-send")
        if not executable:
            return False
        try:
            result = subprocess.run(
                [executable, "--app-name=Scale Switcher", title, body],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("notify-send failed: %s", exc)
            return False
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            log.warning("notify-send exited %s: %s", result.returncode, detail)
            return False
        return True

    def _gio(self, title: str, body: str) -> bool:
        try:
            notification = Gio.Notification.new(title)
            notification.set_body(body)
            self._application.send_notification("scale-switcher", notification)
            return True
        except GLib.Error as exc:
            log.warning("Gio.Notification failed: %s", exc.message)
            return False
