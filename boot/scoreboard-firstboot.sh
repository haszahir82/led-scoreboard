#!/bin/bash
# Stage A: runs once, very early on the first boot, from the SD card's boot
# partition. Invoked by systemd.run= in cmdline.txt (the same hook Raspberry
# Pi Imager uses for its own firstrun.sh).
#
# At this point in the boot there is no network and no user session, so this
# script does almost nothing: it copies the payload off the FAT partition,
# installs a systemd unit that does the real work once the network is up, and
# gets out of the way. Stage B (scoreboard-setup.service) is where the install
# actually happens.
#
# Everything is logged to the boot partition so that a failure can be read by
# putting the card back in a laptop, with no ssh and no screen.

set -u

BOOT="$(dirname "$(readlink -f "$0")")"
LOG="$BOOT/scoreboard-install.log"

log() { echo "[firstboot $(date -u '+%H:%M:%S')] $*" | tee -a "$LOG"; }

# Take our own hook back out of cmdline.txt. Without this the kernel runs stage
# A on every boot, and because the hook asks for a reboot on success that is
# not a wasted minute, it is an infinite reboot loop. Every exit path from here
# on has to go through this, including the ones that give up early.
#
# When Imager owns the hook instead, cmdline.txt names firstrun.sh, the pattern
# does not match, and Imager cleans up after itself as usual.
clear_hook() {
  if grep -q 'systemd.run=[^ ]*scoreboard-firstboot' "$BOOT/cmdline.txt" 2>/dev/null; then
    sed -i 's| systemd.run=[^ ]*scoreboard-firstboot[^ ]*||g; s| systemd.run_success_action=reboot||g; s| systemd.unit=kernel-command-line.target||g' \
      "$BOOT/cmdline.txt" && log "boot hook removed from cmdline.txt"
  fi
}

log "stage A starting"

# Deliberately does NOT resolve the user account here.
#
# This runs in a stripped-down boot target, before cloud-init (or the older
# firstrun.sh) has created the account the card asked for. An earlier version
# read uid 1000 at this point and got whatever placeholder the image ships --
# "pi" on a Trixie image -- then installed everything for that account while
# the real one was created on the next boot with a different uid. The symptom
# was a board that looked installed and answered to nobody.
#
# So the payload goes somewhere root-owned that always exists, and stage B,
# which runs on the next boot after the account exists, decides who owns it.

STAGING=/var/lib/scoreboard
mkdir -p "$STAGING"

ZIP="$(ls "$BOOT"/led-scoreboard*.zip "$BOOT"/scoreboard*.zip 2>/dev/null | head -1)"
if [[ -z "$ZIP" ]]; then
  log "ERROR: no scoreboard zip found on the boot partition; nothing to do"
  clear_hook
  exit 0
fi
log "payload: $(basename "$ZIP")"

cp "$ZIP" "$STAGING/payload.zip"

# Optional: a plain-text file the gift-giver can drop next to the zip to
# preseed the board's name. One line, e.g.  Living Room Scoreboard
if [[ -f "$BOOT/scoreboard-name.txt" ]]; then
  cp "$BOOT/scoreboard-name.txt" "$STAGING/name.txt"
  log "board name preseeded"
fi

if [[ -f "$BOOT/scoreboard-panels.txt" ]]; then
  cp "$BOOT/scoreboard-panels.txt" "$STAGING/panels.txt"
  log "panel count preseeded"
fi

INSTALL_ARGS="--yes"
[[ -f "$BOOT/scoreboard-no-comitup" ]] && INSTALL_ARGS="$INSTALL_ARGS --no-comitup"

# Stage B. Runs as root on the next boot, once cloud-init has created the
# account and the network is up.
cat > /etc/systemd/system/scoreboard-setup.service <<UNIT
[Unit]
Description=First-boot install of the LED scoreboard
After=network-online.target
Wants=network-online.target
ConditionPathExists=/var/lib/scoreboard/payload.zip

[Service]
Type=oneshot
RemainAfterExit=yes
# Runs as root deliberately. The previous version ran as the owning user and
# put "sudo" in front of every write to the log on /boot/firmware, which a
# regular user cannot write. When sudo was unavailable or wanted a password,
# every single log line vanished and the failure looked identical to the
# service never having started. Root writes the log directly; the install
# itself still drops to the user, which is what install.sh expects.
WorkingDirectory=/var/lib/scoreboard
Environment=SCOREBOARD_FIRSTBOOT=1
ExecStart=/usr/local/sbin/scoreboard-stage-b "$INSTALL_ARGS"
TimeoutStartSec=3600
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

cat > /usr/local/sbin/scoreboard-stage-b <<'STAGEB'
#!/bin/bash
# Stage B: unpack and install, once the network is actually up. Runs as root.
set -uo pipefail

BOOTDIR=/boot/firmware
[[ -d "$BOOTDIR" ]] || BOOTDIR=/boot
LOG="$BOOTDIR/scoreboard-install.log"

# No sudo anywhere in here. This is the diagnostic of last resort for a board
# with no screen and no ssh, so writing it must not depend on anything.
log() { echo "[install $(date -u '+%H:%M:%S')] $*" | tee -a "$LOG"; }

# Resolve the account HERE, not in stage A. By now cloud-init has run and the
# account the card asked for exists.
STAGING=/var/lib/scoreboard
USER_NAME="$(getent passwd 1000 | cut -d: -f1)"
USER_HOME="$(getent passwd 1000 | cut -d: -f6)"

log "stage B starting as $(whoami), installing for ${USER_NAME:-?}"

if [[ -z "$USER_NAME" || ! -d "$USER_HOME" ]]; then
  log "ERROR: no usable account (user='${USER_NAME:-}' home='${USER_HOME:-}')"
  exit 1
fi

# Wait for a network that actually carries traffic, not just one that resolves.
#
# An earlier version checked DNS for github.com only. On a freshly associated
# wifi link that can pass while the link still drops connections, and pip talks
# to pypi.org rather than github.com, so `install Pillow` and `clone rgb-matrix`
# both failed on the first boot and then succeeded by hand minutes later. So:
# check every host the install actually needs, and fetch from each rather than
# just resolving it.
# The probe deliberately does NOT use `curl -f`.
#
# -f makes curl fail on any HTTP status of 400 or above, and
# https://files.pythonhosted.org/ has no root document: a bare GET answers 404,
# because it is a CDN for package files and nothing else. So the old probe
# returned failure on a completely healthy network, on every board, every time.
# Stage B then waited out its full five minutes and printed "check the wifi
# settings", which sent people to debug a network that was working. It cost
# days.
#
# Without -f, curl exiting 0 means DNS resolved, TCP connected, TLS verified
# and a whole HTTP response came back. That is precisely the question being
# asked. The status code on the response is irrelevant to it.
probe() {
  curl -sS --max-time 15 -o /dev/null "https://$1" 2>/dev/null
}

# A Pi has no real-time clock. Until NTP syncs, the date is whatever was last
# saved to disk, every certificate reads as not-yet-valid, and every HTTPS
# probe fails for a reason that has nothing to do with the network.
#
# "Is the year plausible" is not a strong enough test, and a real install
# proved it: the board came up about a day behind, sailed through that check
# because 2026 >= 2025, and then apt rejected every Raspberry Pi repository
# with "Not live until 2026-09-18T21:51:36Z" -- the signature's start time was
# still in the Pi's future. TLS was perfectly happy the whole time, because a
# certificate's validity window is months wide and a day's drift sits well
# inside it. Repository signatures are narrow, so apt is the thing that
# notices. Ask whether the clock is actually synchronised.
clock_ok() {
  if command -v timedatectl >/dev/null 2>&1; then
    [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]] \
      && return 0
    return 1
  fi
  # No timedatectl: fall back to the weak test rather than blocking forever.
  [[ "$(date -u +%Y)" -ge 2025 ]]
}

net_ready() {
  local host
  for host in github.com pypi.org files.pythonhosted.org; do
    getent hosts "$host" >/dev/null 2>&1 || return 1
    probe "$host" || return 1
  done
  return 0
}

# Give the clock its own wait, before the network loop, and say what happened.
# It is a separate failure with a separate fix, and rolling it into "no
# network" is how the last one went undiagnosed.
for i in $(seq 1 24); do
  clock_ok && break
  [[ $i -eq 1 ]] && log "waiting for the clock to sync (now $(date -u '+%F %T') UTC)"
  sleep 5
done
if clock_ok; then
  log "clock synced: $(date -u '+%F %T') UTC"
else
  log "WARNING: the clock never synced (now $(date -u '+%F %T') UTC)."
  log "         apt may reject repository signatures as not yet valid."
fi

for i in $(seq 1 60); do
  net_ready && break
  sleep 5
done
if ! net_ready; then
  # Say which layer actually failed. "No network" covers a dead radio, a wrong
  # password, DNS, a wrong clock and a blocked proxy, and naming the wrong one
  # sends people to the wrong place.
  if ! clock_ok; then
    log "ERROR: the clock never synced (now $(date -u '+%F %T') UTC); HTTPS cannot"
    log "       verify certificates against a wrong date. The network may be fine."
  elif ! getent hosts github.com >/dev/null 2>&1; then
    log "ERROR: no DNS after 5 minutes. The wifi password or SSID is likely wrong."
  else
    log "ERROR: DNS works but no host answered after 5 minutes."
    for h in github.com pypi.org files.pythonhosted.org; do
      log "       $h: $(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' \
        "https://$h" 2>&1 | tail -1)"
    done
  fi
  log "       Recover without re-flashing: unzip /var/lib/scoreboard/payload.zip"
  log "       into the home directory and run ./install.sh --yes"

  # Dump enough state to diagnose this from the card alone. Without it, "no
  # network" is a dead end that costs a whole re-flash cycle to learn anything
  # from, and the person holding the card has no screen and no ssh.
  {
    echo
    echo "==== network diagnostics $(date -u '+%F %T') ===="
    echo "--- interfaces ---"
    ip -brief addr 2>&1
    echo "--- wifi radio ---"
    rfkill list 2>&1
    iw reg get 2>&1 | head -5
    echo "--- known connections ---"
    nmcli -t -f NAME,TYPE,DEVICE,STATE con show 2>&1
    echo "--- device status ---"
    nmcli -t -f DEVICE,TYPE,STATE,CONNECTION dev status 2>&1
    echo "--- networks in range (ssid only) ---"
    nmcli -t -f SSID,SIGNAL dev wifi list 2>&1 | head -20
    echo "--- netplan as cloud-init rendered it ---"
    ls -la /etc/netplan/ 2>&1
    # Config files can hold the wifi password, so show structure, not values.
    grep -rvE 'password|psk' /etc/netplan/ 2>&1 | head -40
    echo "--- cloud-init result ---"
    cloud-init status --long 2>&1 | head -20
    journalctl -u cloud-init --no-pager 2>&1 | grep -iE 'error|warn|fail' | head -20
    echo "==== end diagnostics ===="
  } >> "$LOG" 2>&1

  exit 1
fi
log "network up"

# install.sh calls sudo throughout, so the account has to have it without a
# password prompt. Imager's own customisation arranges that; a hand-written
# cloud-init user-data may not. Root can guarantee it here, and a prompt with
# no terminal attached is a hang, not an error message.
if ! runuser -u "$USER_NAME" -- sudo -n true 2>/dev/null; then
  printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$USER_NAME" \
    > /etc/sudoers.d/010-scoreboard-nopasswd
  chmod 440 /etc/sudoers.d/010-scoreboard-nopasswd
  log "granted passwordless sudo to $USER_NAME"
fi

# Pi OS starts its own apt timers on boot (apt-daily, apt-daily-upgrade,
# unattended-upgrades). They hold the dpkg lock, and an install that races them
# fails every apt call -- which then cascades: no git, so no matrix library; no
# pip, so no Pillow; and a service that cannot even import. Everything looked
# like eleven separate failures when it was one lock.
log "waiting for boot-time apt activity to finish"
systemctl stop apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1
systemctl stop apt-daily.service apt-daily-upgrade.service \
  unattended-upgrades.service >/dev/null 2>&1

for i in $(seq 1 60); do
  if ! fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock \
              /var/lib/apt/lists/lock >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
if fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
  log "WARNING: something still holds the dpkg lock after 5 minutes"
  fuser -v /var/lib/dpkg/lock-frontend >>"$LOG" 2>&1
fi

# A failed apt update is the single most common cause of a cascade, so fail
# loudly here rather than eleven steps later.
if ! apt-get update >>"$LOG" 2>&1; then
  log "WARNING: apt-get update failed; package steps will probably fail too"
fi

command -v unzip >/dev/null 2>&1 || apt-get install -y unzip >/dev/null 2>&1

PAYLOAD="$STAGING/payload.zip"
[[ -f "$PAYLOAD" ]] || { log "ERROR: payload missing at $PAYLOAD"; exit 1; }
unzip -oq "$PAYLOAD" -d "$USER_HOME" || { log "ERROR: unpack failed"; exit 1; }
chown -R "$USER_NAME": "$USER_HOME/scoreboard"
chmod +x "$USER_HOME/scoreboard/install.sh" "$USER_HOME/scoreboard/scoreboard" 2>/dev/null
log "unpacked to $USER_HOME/scoreboard"

if [[ -f "$STAGING/name.txt" ]]; then
  mkdir -p "$USER_HOME/scoreboard/state"
  head -1 "$STAGING/name.txt" \
    > "$USER_HOME/scoreboard/state/preseed-name.txt"
fi

if [[ -f "$STAGING/panels.txt" ]]; then
  mkdir -p "$USER_HOME/scoreboard/state"
  head -1 "$STAGING/panels.txt" \
    > "$USER_HOME/scoreboard/state/preseed-panels.txt"
fi

[[ -d "$USER_HOME/scoreboard/state" ]] \
  && chown -R "$USER_NAME": "$USER_HOME/scoreboard/state"

# Run it, and if it fails, run it once more after a pause.
#
# install.sh is idempotent and skips whatever already succeeded, so a second
# attempt costs little and fixes the failure mode seen in practice: a first boot
# where the network is technically up but a few fetches still fail. Every one of
# those runs ended with the same advice -- "ssh in and run install.sh --yes" --
# which the board can perfectly well do for itself.
STATUS=1
for attempt in 1 2; do
  log "running install.sh $* (attempt $attempt)"
  runuser -l "$USER_NAME" -c "cd '$USER_HOME/scoreboard' && ./install.sh $*" 2>&1 \
    | tee -a "$LOG"
  STATUS=${PIPESTATUS[0]}
  [[ $STATUS -eq 0 ]] && break
  if [[ $attempt -eq 1 ]]; then
    log "install reported failures; waiting 60s and retrying once"
    sleep 60
    net_ready || log "note: the network still looks unreliable"
  fi
done

if [[ $STATUS -eq 0 ]]; then
  log "install finished OK"
  rm -f "$PAYLOAD"
  systemctl disable scoreboard-setup.service
else
  log "install exited $STATUS - see above, then run ~/scoreboard/install.sh --yes"
fi
exit $STATUS
STAGEB

chmod +x /usr/local/sbin/scoreboard-stage-b
systemctl enable scoreboard-setup.service >/dev/null 2>&1
log "stage B armed; it will run once the network is up"

clear_hook

exit 0
