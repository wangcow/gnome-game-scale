#!/bin/bash
# Remove Scale Switcher for the current user.
#
#   uninstall.sh          remove the app, keep settings in ~/.config/scale-switcher
#   uninstall.sh --purge  also remove the settings
#
# A running copy is stopped first, which puts the desktop scale back.

set -euo pipefail

APP_ID=com.wangcow.ScaleSwitcher
SERVICE=scale-switcher.service

DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
APP_DIR=$DATA_HOME/scale-switcher
SETTINGS_DIR=$CONFIG_HOME/scale-switcher
SERVICE_FILE=$CONFIG_HOME/systemd/user/$SERVICE

purge=no
for arg in "$@"; do
    case $arg in
        --purge) purge=yes ;;
        -h | --help)
            sed -n '2,7s/^# \{0,1\}//p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg (try --help)" >&2
            exit 2
            ;;
    esac
done

# Stop it first. The app restores the desktop scale when it gets SIGTERM.
if [ -f "$SERVICE_FILE" ] && command -v systemctl >/dev/null; then
    systemctl --user disable --now "$SERVICE" >/dev/null 2>&1 || true
fi
if pkill -TERM -f "$APP_DIR/scale_switcher.py" 2>/dev/null; then
    for _ in 1 2 3 4 5; do
        pgrep -f "$APP_DIR/scale_switcher.py" >/dev/null || break
        sleep 1
    done
fi

rm -f "$SERVICE_FILE"
[ -d "$(dirname "$SERVICE_FILE")" ] && command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
rm -f "$CONFIG_HOME/autostart/scale-switcher.desktop"
rm -f "$DATA_HOME/applications/$APP_ID.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$DATA_HOME/applications" 2>/dev/null || true
rm -rf "$APP_DIR"

if [ "$purge" = yes ]; then
    rm -rf "$SETTINGS_DIR"
    echo "Scale Switcher and its settings were removed."
else
    echo "Scale Switcher was removed. Settings are kept in $SETTINGS_DIR (use --purge to remove them)."
fi
