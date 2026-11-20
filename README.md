# Scale Switcher

A small GNOME app that sets the display scale to **100%** while selected games are running and puts back your usual scale (for example **125%**) when the last one exits.

**Product of the Wangcow Corporation**

It is aimed at GNOME on Wayland. Fedora, Debian, and Arch are all fine when the running session is GNOME. The window is a libadwaita app. The watcher can keep running after the window is closed.

## What it does

- Watches a list of process names you edit in the window.
- When any of them is running, remembers each monitor's scale, switches every monitor to 100%, and shows a notification.
- When the last one exits, puts each monitor back to the scale it had and shows a notification. A laptop at 125% next to an external monitor at 100% comes back exactly that way.
- Keeps 100% while the game runs. If Mutter resets the display (a monitor plugged in or out, the lid, resume from suspend, Settings → Displays), the watcher sets 100% again after the start delay.
- Restores the desktop scale when you quit it, or when systemd stops it.
- Waits briefly before switching: 0.5 seconds before 100%, so the game opens its window at 100%, and 4 seconds before the desktop scale, so a launcher handing off to the game or a game restarting itself does not flicker the desktop. Both are configurable.
- Treats the name `lutris` as a launcher. The Lutris window by itself does nothing. A game counts when Lutris has launched it (see below).

The scale change goes through Mutter's `org.gnome.Mutter.DisplayConfig` D-Bus API (`ApplyMonitorsConfig`, temporary method). Resolution, rotation, and the primary monitor stay as they are. With several monitors side by side or stacked, the ones after a resized monitor are moved so they stay adjacent. The screen can blink once; that is the modeset. `~/.config/monitors.xml` is not rewritten, so the scale you saved in Settings is what comes back at the next login.

100% is always available. If a saved desktop scale is no longer offered for a monitor (its resolution changed), the log says so and that monitor is left alone.

## Supported sessions

Scale Switcher works in any GNOME session, on any distro: Fedora, Debian, Arch, Ubuntu, and others. It talks to Mutter, GNOME's display server, which is the only thing that provides the display API it uses.

On KDE Plasma or any other desktop, Mutter is not running. The app changes nothing, and the window and log say it works only in a GNOME session.

The **Session** row in the window, and the first `Session:` line in the log, show the desktop, the session type, and whether Mutter was found.

## Install

On Fedora:

```bash
sudo dnf install python3-gobject gtk4 libadwaita python3-psutil
```

Those RPMs provide GTK 4, libadwaita, and PyGObject. `psutil` is also listed in `requirements.txt` if you would rather install it with pip:

```bash
python3 -m pip install --user -r requirements.txt
```

Prefer the `python3-psutil` RPM when it is available.

On Arch Linux:

```bash
sudo pacman -S python-gobject gtk4 libadwaita python-psutil
```

On Debian:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-psutil
```

## Run

```bash
./scale_switcher.py
```

From another directory:

```bash
/usr/bin/python3 /path/to/gnome-game-scale/scale_switcher.py
```

Use the system Python (`/usr/bin/python3`). It is the one that has the GTK, libadwaita, and psutil packages above. A `python3` from Homebrew, pyenv, or a virtualenv earlier in `PATH` usually does not.

Closing the window hides it and leaves the watcher running. Open the app again to show the window. Use the menu (or Ctrl+Q) to quit.

`--background` starts the watcher without showing the window. A later launch shows it. Only one instance runs; a second launch raises the first.

Logs go to stdout:

```text
12:01:05 INFO Monitored app started (Cyberpunk2077.exe). Waiting 0.5s before setting 100%
12:01:06 INFO Display scale set to 100% (delay elapsed)
```

## Add an application

1. Open Scale Switcher.
2. Under **Monitored processes**, choose **Add a running app**.
3. Start the game or app. When its name appears, select it.
4. If that app was already open, turn on **Show apps already open** and select it.

The name is saved in the list immediately. The scale changes about a second after the process is actually running, and returns to the desktop scale about 4 seconds after it exits. Processes are checked once a second, so each delay can run up to a second longer.

You can also type a name in **Process name** and press Enter. A pasted path (`/usr/bin/steam` or `C:\Games\Wow.exe`) is stored as the file name. Matching is case-insensitive. The picker only lists your own processes.

### Battle.net and World of Warcraft

- `Wow.exe` (or `WowB.exe`, `WowClassic.exe`) switches only while the game runs.
- `Battle.net.exe` switches while the launcher is open. The launcher minimizes to the tray instead of exiting, so the desktop scale comes back only after you quit it from the tray.
- Do not add `Agent.exe`. It is Blizzard's updater and stays running in the background.

## The process list

Examples: `heroic`, `steam`, `GeForceNOW`, `Cyberpunk2077.exe`.

Linux only keeps 15 characters in `/proc/<pid>/comm`. The watcher also compares the executable name and any `*.exe` on the command line, so a long game name still matches. Wine shows Windows paths there (`C:\Program Files (x86)\...\Battle.net.exe`); the file name after the last `\` is what counts.

`steam` matches the Steam client itself, including when you are only in the library. To limit the switch to a game, add that game's executable instead.

The name `lutris` does not match the Lutris client. While that client is running, a game is detected when:

- a child process is the game, `lutris-wrapper`, Wine, umu, or gamescope, or
- a process has `LUTRIS_GAME_UUID` in its environment (Lutris sets this on the launch; it still works if the game is reparented), or
- a game-like executable appears after Lutris was first seen.

Wine's own leftover processes (`wineserver`, `explorer.exe`, `services.exe`, and similar) are ignored for this automatic detection, because they often outlive the game. Adding one of those names to the list yourself still matches it directly.

Flatpak Lutris is best-effort. The host has to be able to see the game processes. A native Lutris package is the reliable case.

## Settings file

```text
~/.config/scale-switcher/config.json
```

```json
{
  "enabled": true,
  "notifications": true,
  "processes": ["lutris", "heroic"],
  "poll_interval_seconds": 1.0,
  "start_delay_seconds": 0.5,
  "stop_delay_seconds": 4.0
}
```

The window edits `enabled`, `notifications`, `processes`, and the two delays (under **Timing**). Each delay is limited to 0 to 60 seconds:

- `start_delay_seconds`: how long a monitored app has to be running before 100%. It is also the wait before setting 100% again after Mutter resets the display.
- `stop_delay_seconds`: how long no monitored app has to be running before the desktop scale comes back. If a game starts again within this time, the display stays at 100%.

Changes made in the window apply right away. The app reads the file only at startup, so restart it after editing the file by hand (Quit from the menu, or `systemctl --user restart scale-switcher`). `poll_interval_seconds` is file-only. Older versions had one `debounce_seconds` for both directions; it is replaced with the two defaults above on load.

`~/.config/scale-switcher/state.json` remembers whether a game session was active and the scale each monitor had before 100%, so a crash still restores them after the game is gone. Older versions saved full Windows paths in `processes`; those are rewritten to file names on load.

## Notifications

Notifications use `notify-send` when it is installed (`libnotify`), and `Gio.Notification` otherwise. Each one replaces the previous one. The switch in the window turns them off. The log line is still written.

## X11

GNOME 50 no longer has an X11 session. On an older GNOME on X11, the same Mutter call is used, but fractional scales such as 125% are a Wayland feature, so there is usually nothing to switch.

## Start at login

Use either the systemd user service or the autostart desktop file, not both.

Run the commands below from the project directory (the folder that contains `scale_switcher.py`). They substitute that path for `@@PROJECT_DIR@@`.

### systemd user service

```bash
mkdir -p ~/.config/systemd/user
sed "s|@@PROJECT_DIR@@|$PWD|g" data/scale-switcher.service \
  > ~/.config/systemd/user/scale-switcher.service
systemctl --user daemon-reload
systemctl --user enable --now scale-switcher.service
```

Follow the log:

```bash
journalctl --user -u scale-switcher -f
```

Stop it with `systemctl --user disable --now scale-switcher.service`.

The service starts after `graphical-session.target` and talks to the session bus. It does not need `WAYLAND_DISPLAY`.

### Desktop files

Application menu (shows the window). The same command fills in the icon path:

```bash
mkdir -p ~/.local/share/applications
sed "s|@@PROJECT_DIR@@|$PWD|g" data/io.github.scaleswitcher.ScaleSwitcher.desktop \
  > ~/.local/share/applications/io.github.scaleswitcher.ScaleSwitcher.desktop
update-desktop-database ~/.local/share/applications
```

Run that again after an update so the overview uses `data/icons/`. The icon is a small tile and a larger tile, with an arrow from the small one to the large one. A flat version of that mark is used inside the window.

Autostart instead of systemd (starts hidden):

```bash
mkdir -p ~/.config/autostart
sed "s|@@PROJECT_DIR@@|$PWD|g" data/scale-switcher-autostart.desktop \
  > ~/.config/autostart/scale-switcher.desktop
```

## Test buttons

**Test switch to 100%** remembers the current scale and sets 100%. **Restore desktop scale** puts it back. A manual 100% stays until you press Restore or a monitored game starts and exits. While a monitored app is running, the watcher sets 100% again after Restore.

## Tests

```bash
/usr/bin/python3 -m unittest discover -s tests -t .
```

The tests do not talk to Mutter or change the display.

## Wangcow Corporation

Copyright 2026 Wangcow Corporation

**Product of the Wangcow Corporation** — this attribution must be preserved in redistributions and shown in the application About footer. See [NOTICE](NOTICE).

The menu **About Scale Switcher** shows the same lines, with [wangcow.com](https://wangcow.com).
