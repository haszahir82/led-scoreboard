#!/usr/bin/env bash
# Reset a working board into the state it should be in when handed to someone.
#
#   sudo ./prepare-for-gift.sh
#
# Run this LAST, once the board is fully built and tested on your own wifi.
# It forgets your network so the board comes up as a Comitup access point in
# their house, clears the setup so they get the wizard, and wipes the machine
# identity so cloned cards do not collide on the same network.
#
# After this, shut down cleanly and pull the card. Do not boot it again on your
# wifi, or it will re-save your network and the captive portal will not appear.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say()  { printf '\n\033[1;33m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m !\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./prepare-for-gift.sh" >&2
  exit 1
fi

cat <<'WARNING'

This will:
  - forget every saved wifi network on this Pi
  - reset the scoreboard to its first-run setup wizard
  - clear caches, logs, and shell history
  - delete SSH host keys and machine-id so clones get fresh ones

You will lose SSH access to this board until it joins a network again.

WARNING

read -rp "Type PREPARE to continue: " reply
[[ "$reply" == "PREPARE" ]] || { echo "Cancelled."; exit 0; }

# ---------------------------------------------------------------- wifi

say "Forgetting saved wifi networks"
if command -v nmcli >/dev/null 2>&1; then
  # Comitup only falls back to AP mode when there is nothing to connect to,
  # so every saved network has to go, not just yours.
  while read -r name type; do
    [[ "$type" == "802-11-wireless" ]] || continue
    [[ "$name" == comitup* ]] && continue
    echo "  removing $name"
    nmcli connection delete "$name" >/dev/null 2>&1
  done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null | tr ':' ' ')
else
  warn "nmcli not found; if this image uses dhcpcd, clear wpa_supplicant.conf by hand"
fi

rm -f /etc/wpa_supplicant/wpa_supplicant.conf 2>/dev/null

# ---------------------------------------------------------------- scoreboard

say "Resetting the scoreboard to first-run"
systemctl stop scoreboard 2>/dev/null

if [[ -f "$HERE/config.json" ]]; then
  python3 - "$HERE/config.json" <<'PY'
import json, sys
path = sys.argv[1]
try:
    cfg = json.load(open(path))
except Exception:
    cfg = {}

# Keep the hardware settings: those describe the panel, which is not changing
# and which the recipient has no way to work out for themselves. Clear only
# the things that are personal to this build.
cfg.setdefault("setup", {})["complete"] = False
cfg.setdefault("identity", {}).update(name="Scoreboard", hostname="scoreboard")
cfg["location"] = {"zip": "", "latitude": None, "longitude": None,
                   "city": "", "state": "", "resolved_zip": ""}
for key, league in (cfg.get("leagues") or {}).items():
    league["favorites"] = []

json.dump(cfg, open(path, "w"), indent=2)
print("  config reset, panel hardware settings kept")
PY
fi

rm -rf "$HERE/state" "$HERE/logo_cache" "$HERE/out" "$HERE/fixtures" 2>/dev/null
mkdir -p "$HERE/state" "$HERE/logo_cache"
chgrp -R daemon "$HERE" 2>/dev/null
chmod 775 "$HERE" "$HERE/state" "$HERE/logo_cache" 2>/dev/null
[[ -f "$HERE/config.json" ]] && chmod 664 "$HERE/config.json"

say "Resetting hostname to scoreboard"
hostnamectl set-hostname scoreboard 2>/dev/null
sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tscoreboard/' /etc/hosts 2>/dev/null

# ---------------------------------------------------------------- identity
#
# Cloned cards are byte-identical, which means identical SSH host keys and an
# identical machine-id. Two such boards on one network fight over DHCP leases
# and mDNS names, and every SSH client screams about changed host keys. Both
# regenerate on next boot once removed.

say "Clearing machine identity so clones are unique"
rm -f /etc/ssh/ssh_host_* 2>/dev/null
truncate -s 0 /etc/machine-id 2>/dev/null
rm -f /var/lib/dbus/machine-id 2>/dev/null
ln -sf /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null

# ---------------------------------------------------------------- tidy

say "Clearing logs and history"
journalctl --rotate >/dev/null 2>&1
journalctl --vacuum-time=1s >/dev/null 2>&1
find /var/log -type f -name "*.log" -exec truncate -s 0 {} \; 2>/dev/null
rm -f /root/.bash_history /home/*/.bash_history 2>/dev/null
history -c 2>/dev/null

say "Verifying the board still passes its own tests"
sudo -u "#$(stat -c '%u' "$HERE")" python3 -m tools.test_scoreboard >/dev/null 2>&1 \
  && echo "  self-tests pass" || warn "self-tests failed; check before shipping"

cat <<'DONE'

Ready to ship.

  1. sudo shutdown -h now
  2. Wait for the green LED to stop, then pull the power and the card.
  3. Do NOT boot it again on your wifi.

At their house: plug it in, wait about a minute, then on a phone join the
wifi network called scoreboard-<nnn>. The setup page opens on its own. They
pick their home wifi, the board reboots onto it, and then the scoreboard
wizard asks for a name, ZIP code, and teams.

To clone this card for another unit, image it BEFORE first boot:
  macOS:  sudo dd if=/dev/rdiskN of=scoreboard.img bs=4m status=progress
  then write scoreboard.img to the next card with Raspberry Pi Imager.

DONE
