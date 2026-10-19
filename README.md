# Scale Switcher

A small GNOME app that sets the display scale to **100%** while selected games are running and puts it back to **125%** when the last one exits.

It is aimed at Fedora GNOME on Wayland. The window is a libadwaita app. The watcher can keep running after the window is closed.

## What it does

- Watches a list of process names you edit in the window.
- When any of them is running, switches every logical monitor to 100% and shows a notification.
- When the last one exits, switches back to 125% and shows a notification.
- Waits 2 seconds (configurable) before switching, so a game that restarts quickly does not flicker the desktop.
- Treats the name `lutris` as a launcher. The Lutris window by itself does nothing. A game counts when Lutris has launched it (see below).

The scale change goes through Mutter's `org.gnome.Mutter.DisplayConfig` D-Bus API (`ApplyMonitorsConfig`, temporary method). Resolution, position, rotation, and the primary monitor stay as they are. The screen can blink once; that is the modeset. `~/.config/monitors.xml` is not rewritten, so the scale you saved in Settings is what comes back at the next login.

100% and 125% both have to be listed for the current mode. Check Settings → Displays. If 125% is missing, the log says so and the scale is left alone.

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

## Run

```bash
python3 scale_switcher.py
```

From another directory:

```bash
python3 /path/to/gnome-game-scale/scale_switcher.py
```

Closing the window hides it and leaves the watcher running. Open the app again to show the window. Use the menu (or Ctrl+Q) to quit.

`--background` starts the watcher without showing the window. A later launch shows it. Only one instance runs; a second launch raises the first.

Logs go to stdout:

```text
12:01:05 INFO Monitored app started (Cyberpunk2077.exe). Waiting 2.0s before setting 100%
12:01:07 INFO Display scale set to 100% (debounced)
```

## Add an application

1. Open Scale Switcher.
2. Under **Monitored processes**, choose **Add a running app**.
3. Start the game or app. When its name appears, select it.
4. If that app was already open, turn on **Show apps already open** and select it.

The name is saved in the list immediately. The scale changes about 2 seconds after the process is actually running, and returns to 125% about 2 seconds after it exits.

You can also type a name in **Process name** and press Enter. A pasted path is stored as the file name. Matching is case-insensitive.

## The process list

Examples: `heroic`, `steam`, `GeForceNOW`, `Cyberpunk2077.exe`.

Linux only keeps 15 characters in `/proc/<pid>/comm`. The watcher also compares the executable name and any `*.exe` on the command line, so a long game name still matches.

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
  "debounce_seconds": 2.0
}
```

The window edits `enabled`, `notifications`, and `processes`. The two timings can be edited in the file. Debounce is picked up on the next poll. The poll interval is picked up the next time the app starts.

`~/.config/scale-switcher/state.json` remembers whether a game session was active, so a crash can restore 125% after the game is gone.

## Notifications

Notifications use `notify-send` when it is installed (`libnotify`), and `Gio.Notification` otherwise. The switch in the window turns them off. The log line is still written.

## X11

GNOME on X11 still uses Mutter, so the same D-Bus call is tried first. If that fails and the session is X11, the app falls back to `xrandr --scale` (125% UI scale is an `xrandr` scale of 0.8). That fallback can fight the desktop. Fractional scaling in Settings is a Wayland feature. `text-scaling-factor` is not changed, because that setting also applies on Wayland.

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

Application menu (shows the window):

```bash
mkdir -p ~/.local/share/applications
sed "s|@@PROJECT_DIR@@|$PWD|g" data/io.github.scaleswitcher.ScaleSwitcher.desktop \
  > ~/.local/share/applications/io.github.scaleswitcher.ScaleSwitcher.desktop
update-desktop-database ~/.local/share/applications
```

Autostart instead of systemd (starts hidden):

```bash
mkdir -p ~/.config/autostart
sed "s|@@PROJECT_DIR@@|$PWD|g" data/scale-switcher-autostart.desktop \
  > ~/.config/autostart/scale-switcher.desktop
```

## Test buttons

**Test switch to 100%** and **Restore 125%** call Mutter immediately. With the watcher enabled, the next settled poll puts the scale back to whatever the process list requires. Turn the watcher off to hold a manual test.
