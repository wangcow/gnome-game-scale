#!/bin/bash
# Install Scale Switcher for the current user. No root needed.
#
#   ./install.sh                 install or upgrade; asks about starting at login
#   ./install.sh --autostart     also start it at login
#   ./install.sh --no-autostart  do not start it at login
#
# Files go to ~/.local/share/scale-switcher, the app menu entry to
# ~/.local/share/applications. Settings in ~/.config/scale-switcher are
# kept across upgrades. Run uninstall.sh to remove it again.

set -euo pipefail

APP_ID=com.wangcow.ScaleSwitcher
SERVICE=scale-switcher.service
PYTHON=/usr/bin/python3

DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
APP_DIR=$DATA_HOME/scale-switcher
DESKTOP_FILE=$DATA_HOME/applications/$APP_ID.desktop
AUTOSTART_FILE=$CONFIG_HOME/autostart/scale-switcher.desktop
SERVICE_FILE=$CONFIG_HOME/systemd/user/$SERVICE

SOURCE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

autostart=ask
for arg in "$@"; do
    case $arg in
        --autostart) autostart=yes ;;
        --no-autostart) autostart=no ;;
        -h | --help)
            sed -n '2,11s/^# \{0,1\}//p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg (try --help)" >&2
            exit 2
            ;;
    esac
done

have_systemd() {
    command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1
}

missing_packages_hint() {
    local id="" like=""
    if [ -r /etc/os-release ]; then
        id=$(. /etc/os-release && echo "${ID:-}")
        like=$(. /etc/os-release && echo "${ID_LIKE:-}")
    fi
    case " $id $like " in
        *" fedora "* | *" rhel "*)
            echo "  sudo dnf install python3-gobject gtk4 libadwaita python3-psutil" ;;
        *" debian "* | *" ubuntu "*)
            echo "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-psutil" ;;
        *" arch "*)
            echo "  sudo pacman -S python-gobject gtk4 libadwaita python-psutil" ;;
        *)
            echo "  GTK 4, libadwaita, PyGObject, and psutil for $PYTHON" ;;
    esac
}

# 1. Dependencies, checked with the same Python the launcher uses.
if [ ! -x "$PYTHON" ]; then
    echo "Scale Switcher needs $PYTHON, which was not found." >&2
    exit 1
fi
if ! "$PYTHON" - >/dev/null 2>&1 <<'EOF'
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: F401
import psutil  # noqa: F401
EOF
then
    echo "Some system packages are missing. Install them, then run ./install.sh again:" >&2
    missing_packages_hint >&2
    exit 1
fi

if [ "${XDG_CURRENT_DESKTOP:-}" != "" ] && [[ ":${XDG_CURRENT_DESKTOP^^}:" != *":GNOME:"* ]]; then
    echo "Note: this session is $XDG_CURRENT_DESKTOP. Scale Switcher only changes the display on GNOME."
fi

upgrade=no
[ -f "$APP_DIR/scale_switcher.py" ] && upgrade=yes

# 2. Program files. Replace the package directory so removed modules go.
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/scale_switcher" "$APP_DIR/data"
cp -r "$SOURCE_DIR/scale_switcher" "$APP_DIR/"
mkdir -p "$APP_DIR/data"
cp -r "$SOURCE_DIR/data/icons" "$APP_DIR/data/"
cp "$SOURCE_DIR/scale_switcher.py" "$SOURCE_DIR/uninstall.sh" "$SOURCE_DIR/README.md" "$SOURCE_DIR/NOTICE" "$APP_DIR/"
chmod +x "$APP_DIR/scale_switcher.py" "$APP_DIR/uninstall.sh"
find "$APP_DIR/scale_switcher" -name __pycache__ -prune -exec rm -rf {} +

fill() {
    sed "s|@@PROJECT_DIR@@|$APP_DIR|g" "$SOURCE_DIR/data/$1" >"$2"
}

# 3. App menu entry.
mkdir -p "$(dirname "$DESKTOP_FILE")"
fill "$APP_ID.desktop" "$DESKTOP_FILE"
command -v update-desktop-database >/dev/null && update-desktop-database "$(dirname "$DESKTOP_FILE")" 2>/dev/null || true

# 4. Start at login. An upgrade keeps the earlier choice.
if [ "$autostart" = ask ]; then
    if [ -f "$SERVICE_FILE" ] || [ -f "$AUTOSTART_FILE" ]; then
        autostart=yes
    elif [ -t 0 ]; then
        read -r -p "Start Scale Switcher in the background at login? [Y/n] " answer
        case ${answer,,} in n | no) autostart=no ;; *) autostart=yes ;; esac
    else
        autostart=no
    fi
fi

if [ "$autostart" = yes ]; then
    if have_systemd; then
        mkdir -p "$(dirname "$SERVICE_FILE")"
        fill "$SERVICE" "$SERVICE_FILE"
        rm -f "$AUTOSTART_FILE"
        systemctl --user daemon-reload
        systemctl --user enable --quiet "$SERVICE"
        # Start it now, or restart it so an upgrade takes effect.
        systemctl --user restart "$SERVICE"
        how="It starts at login as the systemd user service $SERVICE, and is running now."
    else
        mkdir -p "$(dirname "$AUTOSTART_FILE")"
        fill scale-switcher-autostart.desktop "$AUTOSTART_FILE"
        how="It starts at login. Open it from the app menu to start it now."
    fi
else
    if [ -f "$SERVICE_FILE" ] && have_systemd; then
        systemctl --user disable --now "$SERVICE" >/dev/null 2>&1 || true
        rm -f "$SERVICE_FILE"
        systemctl --user daemon-reload
    fi
    rm -f "$AUTOSTART_FILE"
    how=""
fi

version=$("$PYTHON" -c "import sys; sys.path.insert(0, '$APP_DIR'); import scale_switcher; print(scale_switcher.__version__)")
if [ "$upgrade" = yes ]; then
    echo "Scale Switcher upgraded to $version in $APP_DIR."
else
    echo "Scale Switcher $version installed in $APP_DIR."
fi
[ -n "$how" ] && echo "$how"
echo "Open \"Scale Switcher\" from the app menu to choose your games."
if [ "$upgrade" = yes ] && [ "$autostart" != yes ] && pgrep -f "$APP_DIR/scale_switcher.py" >/dev/null; then
    echo "An older copy is still running. Quit it from its menu and open it again."
fi
echo "To remove it: $APP_DIR/uninstall.sh"
