#!/usr/bin/env bash
# Adds Sentinel to the app grid and puts a launcher icon on the desktop. No root needed.
# Run it again after moving this folder. Remove with: ./install.sh --uninstall
set -euo pipefail

APP_ID="dev.sentinel.Guardian"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
ENTRY="$APPS_DIR/$APP_ID.desktop"
SHORTCUT="$DESKTOP_DIR/$APP_ID.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$ENTRY" "$SHORTCUT"
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
    echo "Sentinel launchers removed. Its log of SSD counters stays in ~/.local/state/sentinel/."
    exit 0
fi

# Sentinel needs GTK 4.14+ (Ubuntu 24.04 or newer), libadwaita and psutil from the Ubuntu repositories.
if ! /usr/bin/python3 - <<'PY' 2>/dev/null
import gi, psutil
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
assert (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 14)
PY
then
    echo "Missing requirements. Install them with:"
    echo "  sudo apt install -y python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-psutil"
    echo "then run ./install.sh again. (Ubuntu 24.04 LTS or newer is required.)"
    exit 1
fi

mkdir -p "$APPS_DIR" "$DESKTOP_DIR"
cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=Sentinel
GenericName=Security & Health Agent
Comment=Live CPU, RAM, GPU, SSD and temperature telemetry plus a full security check of this PC
Exec=/usr/bin/python3 -m sentinel
Path=$HERE
Icon=$HERE/assets/sentinel.svg
Terminal=false
Categories=System;Monitor;Security;
Keywords=security;health;temperature;cpu;gpu;ssd;smart;firewall;monitor;
StartupNotify=true
StartupWMClass=$APP_ID
EOF
chmod +x "$ENTRY"

# The desktop icon is a copy of the menu entry. Marking it trusted lets it launch with a double-click
# instead of showing "Allow Launching" first. (GIO_MODULE_DIR is dropped because snap apps such as
# VS Code set it, which hides the gvfs module that stores this flag.)
cp "$ENTRY" "$SHORTCUT"
chmod +x "$SHORTCUT"
env -u GIO_MODULE_DIR gio set "$SHORTCUT" metadata::trusted true 2>/dev/null \
    || echo "Note: right-click the desktop icon and choose 'Allow Launching' once."

update-desktop-database "$APPS_DIR" 2>/dev/null || true
echo "Sentinel installed:"
echo "  app grid / search : $ENTRY"
echo "  desktop icon      : $SHORTCUT"
