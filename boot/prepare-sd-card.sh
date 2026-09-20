#!/usr/bin/env bash
# Prepare a freshly flashed SD card so the scoreboard installs itself on first
# boot. Run this on your Mac (or any Linux box) with the card still inserted,
# right after Raspberry Pi Imager finishes and before the card goes into the Pi.
#
#   ./prepare-sd-card.sh led-scoreboard-v2.9.zip
#   ./prepare-sd-card.sh led-scoreboard-v2.9.zip --name "Living Room Scoreboard"
#   ./prepare-sd-card.sh led-scoreboard-v2.9.zip --volume /Volumes/bootfs
#
# It writes to the card's small FAT partition (the one that shows up in Finder):
# the zip, the first-boot script, and a one-line hook in cmdline.txt. Nothing on
# the Pi's Linux partition is touched, because a Mac cannot read it.
#
# WIFI AND THE USER ACCOUNT
#
# The Pi needs internet on its first boot to build the LED driver, and current
# Pi OS ships no default account, so the card needs both before it is useful.
# Normally Raspberry Pi Imager writes those from its gear screen. That step has
# a known intermittent bug where the settings never reach the card, so this
# script can write them itself and skip the GUI entirely:
#
#   ./prepare-sd-card.sh board.zip --name "Living Room Scoreboard" \
#       --wifi "HomeNetwork" --wifi-pass "…" \
#       --user hassan --user-pass "…"
#
# That produces a custom.toml, which is exactly what Imager would have written,
# except it is written once and verified rather than left to a race. If the card
# already carries Imager's settings they are left alone.
#
# A card with neither is refused: it would boot to a setup wizard with no
# network and no way in. --force overrides that if you know better.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZIP=""
VOLUME=""
BOARD_NAME=""
PANELS=""
NO_COMITUP=0
FORCE=0
WIFI_SSID=""
WIFI_PASS=""
WIFI_COUNTRY="US"
PI_USER=""
PI_PASS=""
TIMEZONE="America/New_York"
KEYMAP="us"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --volume)      VOLUME="${2:-}"; shift ;;
    --name)        BOARD_NAME="${2:-}"; shift ;;
    --panels)      PANELS="${2:-}"; shift ;;
    --no-comitup)  NO_COMITUP=1 ;;
    --force)       FORCE=1 ;;
    --wifi)        WIFI_SSID="${2:-}"; shift ;;
    --wifi-pass)   WIFI_PASS="${2:-}"; shift ;;
    --country)     WIFI_COUNTRY="${2:-}"; shift ;;
    --user)        PI_USER="${2:-}"; shift ;;
    --user-pass)   PI_PASS="${2:-}"; shift ;;
    --timezone)    TIMEZONE="${2:-}"; shift ;;
    --keymap)      KEYMAP="${2:-}"; shift ;;
    -h|--help)
      awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *)  ZIP="$1" ;;
  esac
  shift
done

BOLD=$'\033[1m'; RED=$'\033[1;31m'; GRN=$'\033[1;32m'; YEL=$'\033[1;33m'; OFF=$'\033[0m'
die() { echo "${RED}error:${OFF} $*" >&2; exit 1; }
ok()  { echo "  ${GRN}ok${OFF}  $*"; }

# ---- locate the payload ----
if [[ -z "$ZIP" ]]; then
  ZIP="$(ls -t "$HERE"/../led-scoreboard*.zip ./led-scoreboard*.zip \
         ~/Downloads/led-scoreboard*.zip 2>/dev/null | head -1)"
fi
[[ -n "$ZIP" && -f "$ZIP" ]] || die "no scoreboard zip given or found (try: $0 path/to/led-scoreboard-v2.9.zip)"
ZIP="$(cd "$(dirname "$ZIP")" && pwd)/$(basename "$ZIP")"

# ---- locate the card's boot partition ----
if [[ -z "$VOLUME" ]]; then
  for candidate in /Volumes/bootfs /Volumes/boot /media/"$USER"/bootfs /media/"$USER"/boot; do
    [[ -d "$candidate" ]] && { VOLUME="$candidate"; break; }
  done
fi
[[ -n "$VOLUME" && -d "$VOLUME" ]] || die "could not find the card's boot volume.
Flash the card with Raspberry Pi Imager first, leave it plugged in, then re-run.
If it is mounted somewhere unusual, pass it: $0 \"$ZIP\" --volume /Volumes/bootfs"
[[ -f "$VOLUME/cmdline.txt" ]] || die "$VOLUME does not look like a Raspberry Pi boot partition (no cmdline.txt)"
[[ -w "$VOLUME" ]] || die "$VOLUME is not writable"

echo "${BOLD}Preparing $VOLUME${OFF}"
echo "  payload: $(basename "$ZIP")"

# ---- make sure the card has wifi and a user account ----
#
# Imager normally supplies these, but its customisation step has a known
# intermittent bug: the settings are entered, the write reports success, and
# nothing reaches the card. Rather than re-flash and hope, this writes the same
# file Imager would have written, deterministically.
#
# Which marker exists depends on Imager's version and the image: custom.toml on
# 1.8 and later, firstrun.sh before that, wpa_supplicant.conf / userconf.txt on
# older images still.
# Which mechanism this image uses. Raspberry Pi OS moved to cloud-init in the
# Trixie-based images (November 2025 onward); those read user-data, meta-data
# and network-config, and ignore custom.toml entirely. Older images read
# custom.toml (Imager 1.8+) or firstrun.sh before that. Writing the wrong one
# is silent: the file just sits on the card unread.
CLOUD_INIT=0
if grep -qs 'ds=nocloud' "$VOLUME/cmdline.txt" || [[ -e "$VOLUME/meta-data" ]]; then
  CLOUD_INIT=1
fi

CUSTOMISED=0
for marker in user-data meta-data network-config custom.toml firstrun.sh \
              wpa_supplicant.conf userconf.txt; do
  [[ -s "$VOLUME/$marker" ]] && CUSTOMISED=1
done

if [[ -n "$WIFI_SSID" || -n "$PI_USER" ]]; then
  # Explicit credentials win. Earlier this deferred to any settings Imager had
  # written, which was wrong: Imager's file may be missing the SSH switch or
  # carry a stale network, and deferring to it silently ignored what was asked
  # for on the command line. Passing --wifi/--user is a statement of intent, so
  # honour it and keep the old file beside it.
  # Imager's network-config is worth keeping when it already names this same
  # network. Its wifi half is the part that works; what it tends to miss is the
  # SSH switch, which lives in user-data. Substituting a hand-written netplan
  # for a working one just to change a setting in a different file is how you
  # trade a login problem for a no-network problem.
  KEEP_NETCFG=0
  if [[ $CLOUD_INIT -eq 1 && -n "$WIFI_SSID" && -s "$VOLUME/network-config" ]]; then
    if grep -qF "$WIFI_SSID" "$VOLUME/network-config"; then
      KEEP_NETCFG=1
    fi
  fi

  if [[ $CUSTOMISED -eq 1 ]]; then
    SET_ASIDE=(user-data meta-data custom.toml firstrun.sh
               wpa_supplicant.conf userconf.txt)
    [[ $KEEP_NETCFG -eq 0 ]] && SET_ASIDE+=(network-config)
    for marker in "${SET_ASIDE[@]}"; do
      if [[ -s "$VOLUME/$marker" ]]; then
        mv "$VOLUME/$marker" "$VOLUME/$marker.imager-bak" \
          && ok "set aside Imager's $marker (kept as $marker.imager-bak)"
      fi
    done
    [[ $KEEP_NETCFG -eq 1 ]] && \
      ok "kept Imager's network-config: it already names $WIFI_SSID"
    CUSTOMISED=0
  fi
  if true; then
    [[ -n "$WIFI_SSID" && -z "$WIFI_PASS" ]] && die "--wifi given without --wifi-pass"
    [[ -n "$PI_USER" && -z "$PI_PASS" ]] && die "--user given without --user-pass"
    [[ -z "$PI_USER" ]] && die "--wifi needs --user too: current Pi OS has no default account,
so a card with wifi but no login is still a card you cannot reach."

    # Hash the login password if the local openssl can. macOS ships LibreSSL,
    # which may not implement -5; falling back to plain text is not ideal but
    # it is what Imager does when asked, and it beats an unusable card. The
    # wifi password stays plain either way, because the encrypted form is
    # reported not to apply reliably.
    PASS_FIELD="$PI_PASS"
    PASS_ENCRYPTED=false
    if HASHED="$(openssl passwd -5 "$PI_PASS" 2>/dev/null)" && [[ -n "$HASHED" ]]; then
      PASS_FIELD="$HASHED"
      PASS_ENCRYPTED=true
    fi

    # Match the hostname to the board name so the Pi answers at the right
    # .local address from its very first boot, rather than only after the
    # installer renames it. Same rules as the Python slugify: apostrophes
    # vanish, everything else non-alphanumeric becomes a single dash.
    HOSTNAME="scoreboard"
    if [[ -n "$BOARD_NAME" ]]; then
      SLUG="$(printf '%s' "$BOARD_NAME" \
        | tr '[:upper:]' '[:lower:]' \
        | sed "s/'//g; s/[^a-z0-9]\{1,\}/-/g; s/^-//; s/-$//")"
      [[ -n "$SLUG" ]] && HOSTNAME="$SLUG"
    fi

    umask 077
    if [[ $CLOUD_INIT -eq 1 ]]; then
      # cloud-init (Raspberry Pi OS Trixie and later). Three files, and all
      # three have to be present: meta-data is what tells cloud-init to look
      # at the other two.
      cat > "$VOLUME/meta-data" <<META
instance-id: scoreboard-$(date +%s)
local-hostname: $HOSTNAME
META

      cat > "$VOLUME/user-data" <<USERDATA
#cloud-config
# Written by prepare-sd-card.sh.
hostname: $HOSTNAME
manage_etc_hosts: true

users:
  - name: $PI_USER
    plain_text_passwd: "$PI_PASS"
    lock_passwd: false
    shell: /bin/bash
    groups: [adm, dialout, cdrom, sudo, audio, video, plugdev, games, users, input, netdev, gpio, i2c, spi]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]

ssh_pwauth: true

# Imager's user-data carries these and ours replaces the whole file, so leaving
# them out silently reverted the board to the image default. The symptom was
# game times hours off, which reads as a bug in the scoreboard rather than a
# missing line here.
timezone: $TIMEZONE
keyboard:
  layout: $KEYMAP

# Belt and braces: the ssh module leaves the service alone on some images, and
# a board that joins the network but refuses every login is the single hardest
# failure to diagnose from the outside.
runcmd:
  - [ systemctl, enable, --now, ssh ]
USERDATA

      if [[ ${KEEP_NETCFG:-0} -eq 1 ]]; then
        : # Imager's network-config stays exactly as it is.
      else
      cat > "$VOLUME/network-config" <<NETCFG
# Netplan, consumed by cloud-init on first boot.
version: 2
wifis:
  renderer: NetworkManager
  wlan0:
    dhcp4: true
    optional: true
    access-points:
      "$WIFI_SSID":
        password: "$WIFI_PASS"
NETCFG
      fi

      for f in meta-data user-data network-config; do
        [[ -s "$VOLUME/$f" ]] || die "could not write $f to $VOLUME"
      done

      # Without a regulatory domain the radio stays off, and Imager normally
      # supplies it on the kernel command line rather than in netplan.
      if ! grep -qs 'ieee80211_regdom' "$VOLUME/cmdline.txt"; then
        CMD="$(tr -d '\n' < "$VOLUME/cmdline.txt")"
        printf '%s cfg80211.ieee80211_regdom=%s\n' "$CMD" "$WIFI_COUNTRY" \
          > "$VOLUME/cmdline.txt"
      fi
      if [[ ${KEEP_NETCFG:-0} -eq 1 ]]; then
        WROTE="cloud-init (user-data, meta-data; kept Imager's network-config)"
      else
        WROTE="cloud-init (user-data, meta-data, network-config)"
      fi
    else
      cat > "$VOLUME/custom.toml" <<TOML
# Written by prepare-sd-card.sh. This is the same file Raspberry Pi Imager's
# customisation screen produces, and Pi OS consumes it on the first boot.
config_version = 1

[system]
hostname = "$HOSTNAME"

[user]
name = "$PI_USER"
password = "$PASS_FIELD"
password_encrypted = $PASS_ENCRYPTED

[ssh]
enabled = true
password_authentication = true

[wlan]
ssid = "$WIFI_SSID"
password = "$WIFI_PASS"
password_encrypted = false
hidden = false
country = "$WIFI_COUNTRY"

[locale]
keymap = "$KEYMAP"
timezone = "$TIMEZONE"
TOML
      [[ -s "$VOLUME/custom.toml" ]] || die "could not write custom.toml to $VOLUME"
      WROTE="custom.toml"
    fi
    umask 022
    CUSTOMISED=1
    if [[ $PASS_ENCRYPTED == true && $CLOUD_INIT -eq 0 ]]; then
      ok "wifi and login written to $WROTE (login password hashed)"
    else
      ok "wifi and login written to $WROTE"
      if [[ $CLOUD_INIT -eq 1 ]]; then
        echo "  ${YEL}note${OFF}  cloud-init takes the login password in plain text, so it is"
        echo "        readable in user-data on the card."
      else
        echo "  ${YEL}note${OFF}  this openssl cannot hash passwords, so the login password is"
        echo "        in plain text in custom.toml on the card."
      fi
      echo "        This is a FAT partition, so file permissions do not apply."
      echo "        Delete those files from the card after the first boot if"
      echo "        that matters to you."
    fi
  fi
fi

if [[ $CUSTOMISED -eq 0 && $FORCE -eq 0 ]]; then
  die "this card has no wifi and no user account.

Nothing on $VOLUME matches custom.toml, firstrun.sh, wpa_supplicant.conf or
userconf.txt. Current Pi OS ships no default login, so this card would boot to
a setup wizard with no network, no keyboard and no way in.

Two ways forward, and the second is the reliable one:

  1. Re-flash, and answer Yes to \"apply OS customisation settings\".

  2. Skip Imager's customisation entirely and let this script write it:

       $(basename "$0") \"$ZIP\" --name \"Living Room Scoreboard\" \\
         --wifi \"YourNetwork\" --wifi-pass \"…\" \\
         --user yourname --user-pass \"…\"

Imager's customisation step has a known intermittent bug where the settings
never reach the card, so if this is not your first attempt, use option 2.

If you are certain the card is fine, re-run with --force."
elif [[ $CUSTOMISED -eq 0 ]]; then
  echo "  ${YEL}note${OFF}  no wifi or user account found, continuing because --force was given"
fi

# ---- copy the pieces ----
cp "$ZIP" "$VOLUME/$(basename "$ZIP")" || die "could not copy the zip"
ok "zip copied"

cp "$HERE/scoreboard-firstboot.sh" "$VOLUME/scoreboard-firstboot.sh" || die "could not copy the first-boot script"
ok "first-boot script copied"

# How many panels are wired left to right. HUB75 has no way to report this --
# it is a dumb shift register with no back channel -- so the board has to be
# told once, and the only person who knows is whoever assembled it. Setting it
# here means a two-panel gift unit is correct the first time it lights up,
# rather than showing the same game twice until someone finds a numeric field
# in the settings page.
if [[ -n "$PANELS" ]]; then
  if [[ "$PANELS" =~ ^[1-8]$ ]]; then
    printf '%s\n' "$PANELS" > "$VOLUME/scoreboard-panels.txt"
    ok "panel count preseeded: $PANELS"
  else
    die "--panels takes a whole number from 1 to 8 (got '$PANELS')"
  fi
fi

if [[ -n "$BOARD_NAME" ]]; then
  printf '%s\n' "$BOARD_NAME" > "$VOLUME/scoreboard-name.txt"
  ok "board name preseeded: $BOARD_NAME"
fi
[[ $NO_COMITUP -eq 1 ]] && { : > "$VOLUME/scoreboard-no-comitup"; ok "captive portal disabled"; }

# ---- hook it into the boot ----
# Pi OS mounts this partition at /boot/firmware (bookworm and later) or /boot
# (older). Imager's own firstrun.sh uses /boot/firmware, so match whatever the
# card already says rather than guessing.
BOOTPATH=/boot/firmware
grep -qs '/boot/firmware' "$VOLUME/cmdline.txt" "$VOLUME/firstrun.sh" 2>/dev/null || {
  [[ -f "$VOLUME/firstrun.sh" ]] && grep -qs '=/boot/' "$VOLUME/firstrun.sh" && BOOTPATH=/boot
}

if grep -q 'scoreboard-firstboot' "$VOLUME/cmdline.txt" 2>/dev/null || \
   { [[ -f "$VOLUME/firstrun.sh" ]] && grep -q 'scoreboard-firstboot' "$VOLUME/firstrun.sh"; }; then
  ok "boot hook already present"
elif [[ -f "$VOLUME/firstrun.sh" ]]; then
  # Imager already owns the systemd.run hook. Ride along inside its script
  # rather than fighting it for cmdline.txt: insert our call just before it
  # cleans itself up.
  TMP="$VOLUME/.firstrun.new"
  INSERTED=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ $INSERTED -eq 0 && "$line" == rm\ -f*firstrun* ]]; then
      printf '%s\n' "bash $BOOTPATH/scoreboard-firstboot.sh || true" >> "$TMP"
      INSERTED=1
    fi
    printf '%s\n' "$line" >> "$TMP"
  done < "$VOLUME/firstrun.sh"
  if [[ $INSERTED -eq 0 ]]; then
    printf '%s\n' "bash $BOOTPATH/scoreboard-firstboot.sh || true" >> "$TMP"
  fi
  mv "$TMP" "$VOLUME/firstrun.sh" || die "could not update firstrun.sh"
  ok "hooked into Imager's first-run script"
else
  # No Imager customisation: claim the systemd.run hook ourselves.
  cp "$VOLUME/cmdline.txt" "$VOLUME/cmdline.txt.bak"
  CMD="$(tr -d '\n' < "$VOLUME/cmdline.txt")"
  printf '%s systemd.run=%s/scoreboard-firstboot.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target\n' \
    "$CMD" "$BOOTPATH" > "$VOLUME/cmdline.txt" || die "could not update cmdline.txt"
  ok "boot hook added to cmdline.txt (original saved as cmdline.txt.bak)"
fi

cat <<EOF

${BOLD}Done.${OFF} Eject the card, put it in the Pi, plug in the panel and power.

  First boot   about a minute, then it reboots itself.
  Second boot  the install runs on its own. On a Zero 2 W allow 15-20 minutes;
               a Pi 4 is nearer 5. The board lights up when it finishes.

Progress is written to ${BOLD}scoreboard-install.log${OFF} on this same volume, so if
nothing happens you can put the card back in this Mac and read what went wrong.
EOF
