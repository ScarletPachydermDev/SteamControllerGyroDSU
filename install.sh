#!/usr/bin/env bash
# Install (or update) the Steam Controller gyro DSU server and its user
# service. Safe to re-run: it overwrites the script and restarts the
# service. Needs no root, since SteamOS already grants the user access to
# the controller's HID device.
set -euo pipefail

REPO=https://raw.githubusercontent.com/ScarletPachydermDev/SteamControllerGyroDSU/master
BIN="$HOME/.local/bin/steam-controller-dsu"
UNIT="$HOME/.config/systemd/user/steam-controller-dsu.service"

fetch() {  # fetch <url> <dest>
    mkdir -p "$(dirname "$2")"
    if [ -f "$(dirname "$0")/$(basename "$1")" ]; then
        install -m "$3" "$(dirname "$0")/$(basename "$1")" "$2"   # running from a clone
    else
        curl -fsSL "$1" -o "$2.new" && install -m "$3" "$2.new" "$2" && rm -f "$2.new"
    fi
}

echo "Installing the Steam Controller gyro DSU server..."
fetch "$REPO/dsu_server.py" "$BIN" 755
fetch "$REPO/steam-controller-dsu.service" "$UNIT" 644

systemctl --user daemon-reload
# Clears any failed state from an earlier version, which otherwise
# refuses to start with "start request repeated too quickly".
systemctl --user reset-failed steam-controller-dsu 2>/dev/null || true
systemctl --user enable --now steam-controller-dsu

# The Steam Deck's own server, if it is installed here, holds the same UDP
# port without being able to read this controller.
if systemctl --user is-enabled sdgyrodsu >/dev/null 2>&1; then
    echo "Disabling sdgyrodsu (the Steam Deck server) so it stops holding port 26760."
    systemctl --user disable --now sdgyrodsu || true
fi

sleep 2
if systemctl --user is-active --quiet steam-controller-dsu; then
    echo "Done. Motion is served on UDP port 26760."
    echo "Point your emulator's motion source at 127.0.0.1:26760."
else
    echo "The service did not start. Its log:" >&2
    journalctl --user -u steam-controller-dsu -n 20 --no-pager >&2
    exit 1
fi
