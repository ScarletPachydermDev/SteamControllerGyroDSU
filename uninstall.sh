#!/usr/bin/env bash
# Remove the server and its service. Leaves nothing behind.
set -u
systemctl --user disable --now steam-controller-dsu 2>/dev/null
rm -f "$HOME/.config/systemd/user/steam-controller-dsu.service" "$HOME/.local/bin/steam-controller-dsu"
systemctl --user daemon-reload
echo "Removed the Steam Controller gyro DSU server."
