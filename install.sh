#!/usr/bin/env bash
# One-command install for the LED scoreboard.
#
#   ./install.sh                      interactive, from this directory
#   ./install.sh --yes                no prompts, sensible defaults
#   ./install.sh --yes --no-comitup   skip the wifi captive portal
#   ./install.sh --yes --manifest-url https://.../manifest.json --channel beta
#                                     arm nightly self-update in one pass
#   ./install.sh --repo <git-url>     clone first, then install
#   ./install.sh --zip ~/board.zip    unpack a zip first, then install
#
# Ends with a running board and a URL, or a list of exactly what failed.
# Safe to run repeatedly: every step checks before it acts.

set -uo pipefail          # deliberately NOT -e; see step()

ASSUME_YES=0
WITH_COMITUP=""
DO_START=1
REPO=""
ZIP=""
TARGET="$HOME/scoreboard"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes)      ASSUME_YES=1 ;;
    --comitup)     WITH_COMITUP=1 ;;
    --no-comitup)  WITH_COMITUP=0 ;;
    --no-start)    DO_START=0 ;;
    --manifest-url) MANIFEST_URL_ARG="${2:-}"; shift ;;
    --channel)      UPDATE_CHANNEL_ARG="${2:-}"; shift ;;
    --repo)        REPO="${2:-}"; shift ;;
    --zip)         ZIP="${2:-}"; shift ;;
    --dir)         TARGET="${2:-}"; shift ;;
    -h|--help)
      awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
      exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

BOLD=$'\033[1m'; YEL=$'\033[1;33m'; RED=$'\033[1;31m'; GRN=$'\033[1;32m'; OFF=$'\033[0m'
FAILED=()
STEP=0

say()  { STEP=$((STEP+1)); printf '\n%s[%d]%s %s\n' "$YEL" "$STEP" "$OFF" "$*"; }
ok()   { printf '    %s✓%s %s\n' "$GRN" "$OFF" "$*"; }
warn() { printf '    %s!%s %s\n' "$RED" "$OFF" "$*"; }

# Every step reports and continues. An earlier version used `set -e`, so the
# first failure killed the script silently partway through and left a board
# that looked installed but had no config and no service. The summary at the
# end is the contract: if it is empty, the install is complete.
step() {
  local label="$1"; shift
  if "$@" >/tmp/scoreboard-install.log 2>&1; then
    ok "$label"
    return 0
  fi
  warn "$label failed"
  sed 's/^/      /' /tmp/scoreboard-install.log | tail -4
  FAILED+=("$label")
  return 1
}

ask() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  local reply
  read -rp "    $1 [Y/n] " reply
  [[ -z "$reply" || "${reply,,}" == "y" ]]
}

if [[ $EUID -eq 0 ]]; then
  echo "Run as your normal user, not with sudo. It calls sudo where needed." >&2
  exit 1
fi

printf '%s\nLED Scoreboard installer%s\n' "$BOLD" "$OFF"

# --------------------------------------------------------------- 1. sources

if [[ -n "$REPO" ]]; then
  say "Cloning $REPO"
  if [[ -d "$TARGET/.git" ]]; then
    step "pull latest" git -C "$TARGET" pull --ff-only
  else
    step "clone" git clone --depth 1 "$REPO" "$TARGET"
  fi
  cd "$TARGET" || exit 1
elif [[ -n "$ZIP" ]]; then
  say "Unpacking $ZIP"
  command -v unzip >/dev/null || step "install unzip" sudo apt-get install -y unzip
  step "unpack" unzip -oq "$ZIP" -d "$(dirname "$TARGET")"
  cd "$TARGET" || exit 1
else
  cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
fi

HERE="$PWD"
if [[ ! -f main.py ]]; then
  echo "No main.py in $HERE. Use --zip or --repo, or run this from the project." >&2
  exit 1
fi
chmod +x install.sh prepare-for-gift.sh scoreboard 2>/dev/null
ok "project at $HERE"

# --------------------------------------------------------------- 2. packages

say "System packages"

# A third-party repo that apt cannot verify fails EVERY update on Trixie, not
# just its own line, so one bad signing key takes out python3-pillow, git and
# build-essential along with it. A previous run of this script could have left
# exactly that behind, so check before trusting apt with anything.
# A wrong clock makes apt reject every repository, and the message it prints
# for it -- "Sub-process /usr/bin/sqv returned an error code (1)" -- names the
# verification tool rather than the cause. The useful half is buried further
# along the same line: "Not live until <timestamp>", meaning the signature does
# not start being valid until a moment still in this Pi's future. Check for it
# before blaming a third-party repo, because the fix is completely different.
#
# Note the exit code is not enough on its own. apt-get update returns 0 when a
# repository fails signature verification -- it prints W: lines, falls back to
# the previous index, and calls that success -- so a board can sail past this
# with package lists days out of date. The log has to be read.
sudo apt-get update >/tmp/scoreboard-aptcheck.log 2>&1
APT_RC=$?
if [[ $APT_RC -ne 0 ]] \
   || grep -qiE 'not live until|sqv returned an error|NO_PUBKEY|not signed' \
        /tmp/scoreboard-aptcheck.log; then
  if grep -qiE 'not live until|sqv returned an error' /tmp/scoreboard-aptcheck.log; then
    warn "apt is rejecting repository signatures because this Pi's clock is wrong"
    grep -oiE 'not live until [^ ]*' /tmp/scoreboard-aptcheck.log | head -1 | sed 's/^/      /'
    echo "      now: $(date -u '+%F %T') UTC. A Pi has no battery-backed clock,"
    echo "      so the date is wrong until NTP catches up."
    for _ in $(seq 1 24); do
      [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]] && break
      sleep 5
    done
    if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]]; then
      ok "clock synced ($(date -u '+%F %T') UTC); retrying apt"
      sudo apt-get update >/tmp/scoreboard-aptcheck.log 2>&1
    else
      warn "the clock still has not synced; package steps may fail"
      FAILED+=("clock never synced, apt cannot verify signatures")
    fi
  fi
  if grep -qiE 'comitup|not signed|missing key|NO_PUBKEY' /tmp/scoreboard-aptcheck.log; then
    warn "a third-party apt repo is unverifiable and is blocking all updates"
    sed 's/^/      /' /tmp/scoreboard-aptcheck.log | grep -iE '^ +(E|W):' | head -3
    sudo rm -f /etc/apt/sources.list.d/comitup.list \
               /etc/apt/sources.list.d/davesteele-comitup*.list
    if sudo apt-get update >/dev/null 2>&1; then
      ok "removed it; apt works again (Comitup is re-added properly later)"
    fi
  fi
fi

step "apt update" sudo apt-get update
step "apt install" sudo apt-get install -y \
  python3-dev python3-pip python3-pillow python3-requests python3-flask \
  git build-essential avahi-daemon unzip

# --------------------------------------------------------------- 3. python
#
# The board runs as root for GPIO, so dependencies must be visible to root.
# Installing them as the invoking user puts them in ~/.local, where root
# cannot see them, and the only symptom is the web UI silently not starting.

say "Python dependencies (for root, which is what runs the board)"
PIP_FLAGS=()
# Captured rather than piped straight into grep -q. With `set -o pipefail`,
# grep -q exits on its first match and closes the pipe, which can kill the
# writer with SIGPIPE and make the pipeline report failure *because* the
# pattern matched. It survives here only because pip's help text fits in the
# pipe buffer, which is not a guarantee worth relying on.
PIP_HELP="$(sudo python3 -m pip install --help 2>/dev/null || true)"
if grep -q break-system-packages <<<"$PIP_HELP"; then
  PIP_FLAGS+=(--break-system-packages)
fi
if ! sudo python3 -m pip --version >/dev/null 2>&1; then
  warn "root has no pip, bootstrapping it"
  PY_TAG="$(python3 -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))')"
  for url in "https://bootstrap.pypa.io/pip/${PY_TAG}/get-pip.py" \
             "https://bootstrap.pypa.io/get-pip.py"; do
    if curl -fsSL "$url" -o /tmp/get-pip.py 2>/dev/null; then
      step "bootstrap pip" sudo python3 /tmp/get-pip.py && break
    fi
  done
fi
for mod in flask requests PIL; do
  pkg="$mod"; [[ $mod == PIL ]] && pkg=Pillow
  if sudo python3 -c "import $mod" 2>/dev/null; then
    ok "$pkg present"
  else
    step "install $pkg" sudo python3 -m pip install "${PIP_FLAGS[@]}" "$pkg"
  fi
done

# --------------------------------------------------------------- 4. matrix

say "RGB matrix library"
if sudo python3 -c "import rgbmatrix" 2>/dev/null; then
  ok "rgbmatrix already installed"
else
  if [[ ! -e matrix/README.md ]]; then
    rm -rf matrix
    step "clone rgb-matrix" git clone --depth 1 \
      https://github.com/hzeller/rpi-rgb-led-matrix.git matrix
  fi

  # Upstream overhauled the Python bindings in February 2026: the build moved to
  # scikit-build-core plus cmake, driven by pyproject.toml, and the old
  # `make build-python` target was deleted outright -- the top-level Makefile
  # now carries a comment saying Python is not handled there. A checkout from
  # before that still has bindings/python/Makefile, so support both rather than
  # assuming either.
  if [[ -f matrix/pyproject.toml ]]; then
    step "matrix build dependencies" sudo apt-get install -y \
      python-dev-is-python3 python3-pil cython3 cmake
    echo "    compiling, several minutes on a Zero or a 3A+"
    step "build bindings" sudo python3 -m pip install "${PIP_FLAGS[@]}" ./matrix
  elif [[ -f matrix/bindings/python/Makefile ]]; then
    echo "    older checkout, using the Makefile build"
    step "matrix build dependencies" sudo apt-get install -y \
      python3-dev cython3
    step "build bindings" bash -c \
      "cd matrix/bindings/python && make build-python PYTHON=\$(command -v python3) \
         && sudo make install-python PYTHON=\$(command -v python3)"
  else
    warn "the matrix checkout has neither pyproject.toml nor a python Makefile"
    echo "      rm -rf matrix, then re-run ./install.sh"
    FAILED+=("matrix source layout unrecognised")
  fi

  # Say so here rather than letting it surface as a traceback at service start.
  if sudo python3 -c "import rgbmatrix" 2>/dev/null; then
    ok "rgbmatrix importable by root"
  else
    warn "rgbmatrix still not importable; the panel will stay dark"
    FAILED+=("rgbmatrix not installed")
  fi
fi

# --------------------------------------------------------------- 5. config

say "Configuration"
if [[ -f config.json ]]; then
  ok "config.json already exists, left alone"
else
  cp config.json.example config.json && ok "created config.json"
fi

# A name dropped on the SD card by prepare-sd-card.sh --name. Applying it here
# means a gift board already answers to its own <name>.local the first time it
# is plugged in, before anyone opens the setup page.
mkdir -p state
# Panel count, dropped on the card by prepare-sd-card.sh --panels. HUB75 cannot
# report how many panels are attached -- it is a shift register with no back
# channel -- so the number has to come from whoever wired the thing. Applying it
# here means a two-panel unit is right the first time it lights up, instead of
# showing the same game on both halves until someone finds the setting.
if [[ -f state/preseed-panels.txt ]]; then
  WANT_PANELS="$(head -1 state/preseed-panels.txt | tr -dc '0-9')"
  if [[ -n "$WANT_PANELS" ]] && [[ "$WANT_PANELS" -ge 1 ]] \
     && [[ "$WANT_PANELS" -le 8 ]]; then
    if python3 - "$WANT_PANELS" <<'PANELS'
import json, sys
n = int(sys.argv[1])
with open("config.json") as fh:
    cfg = json.load(fh)
cfg.setdefault("matrix", {})["chain_length"] = n
with open("config.json", "w") as fh:
    json.dump(cfg, fh, indent=2)
PANELS
    then
      ok "panel count set to $WANT_PANELS ($((WANT_PANELS * 64))x32)"
      rm -f state/preseed-panels.txt
    else
      FAILED+=("apply panel count")
    fi
  else
    FAILED+=("panel count in state/preseed-panels.txt is not 1-8")
  fi
fi

if [[ -f state/preseed-name.txt ]]; then
  PRESEED="$(head -1 state/preseed-name.txt)"
  if python3 - "$PRESEED" <<'PY'
import json, sys
sys.path.insert(0, ".")
from data import identity
name = sys.argv[1].strip()
if name:
    with open("config.json") as fh:
        cfg = json.load(fh)
    cfg.setdefault("identity", {})["name"] = name
    cfg["identity"]["hostname"] = identity.slugify(name)
    with open("config.json", "w") as fh:
        json.dump(cfg, fh, indent=2)
    open("state/hostname.request", "w").write(identity.slugify(name))
PY
  then
    ok "board named \"$PRESEED\""
    rm -f state/preseed-name.txt
  else
    warn "could not apply the preseeded name; set it in the web UI"
  fi
fi

# --------------------------------------------------------------- 6. perms
#
# hzeller's library setuids to `daemon` after claiming GPIO, so everything the
# app writes afterwards has to be group-writable by that user. Skipping this
# presents as "permission denied" when saving from the web page.

# Logos are cached as finished 20px bitmaps, so a change to how they are
# processed does not reach a board that already has a cache: it keeps serving
# the old pictures forever. That is how a batch of white college logos survived
# the fix for it. The cache costs a few seconds to refill on demand, so an
# upgrade simply throws it away.
if [[ -d logo_cache ]] && [[ -n "$(ls -A logo_cache 2>/dev/null)" ]]; then
  say "Clearing the cached logo bitmaps so they re-render"
  step "clear logo cache" sudo rm -rf "$HERE/logo_cache"
fi

say "Permissions for the privilege drop"
mkdir -p state logo_cache
step "group ownership" sudo chgrp -R daemon "$HERE"
step "directory modes" sudo chmod 775 "$HERE" "$HERE/state" "$HERE/logo_cache"
[[ -f config.json ]] && step "config mode" sudo chmod 664 "$HERE/config.json"

# Group ownership on the project is not enough on its own: `daemon` also has to
# be able to WALK to it. Debian made the default home-directory mode 0750 in
# Bookworm, so /home/<user> blocks everyone outside the user's own group, and
# every font and logo under it becomes unreachable the moment the library drops
# privileges. PIL reports that as "OSError: cannot open resource", which reads
# like a missing file rather than a permission problem, so this is worth doing
# explicitly rather than hoping.
#
# Only the execute bit is added: other users may pass through these directories,
# not list them.
TRAVERSE="$HERE"
while [[ "$TRAVERSE" != "/" && -n "$TRAVERSE" ]]; do
  TRAVERSE="$(dirname "$TRAVERSE")"
  [[ "$TRAVERSE" == "/" ]] && break
  if ! sudo -u daemon test -x "$TRAVERSE" 2>/dev/null; then
    sudo chmod o+x "$TRAVERSE" \
      && ok "made $TRAVERSE traversable by daemon"
  fi
done

# Prove it rather than assume it: read an actual font as daemon, which is the
# exact operation that was failing.
FONT_PROBE="$(ls "$HERE"/fonts/* 2>/dev/null | head -1)"
if [[ -n "$FONT_PROBE" ]]; then
  if sudo -u daemon cat "$FONT_PROBE" >/dev/null 2>&1; then
    ok "daemon can read the fonts"
  else
    warn "daemon still cannot read $FONT_PROBE"
    echo "      the panel will start and then die on the first text it draws"
    FAILED+=("daemon cannot read project files")
  fi
fi

# --------------------------------------------------------------- 6b. audio
#
# The panel's hardware-PWM timing and the Pi's onboard sound driver want the
# same peripheral. With snd_bcm2835 loaded the matrix library REFUSES to start
# on any mapping that uses PWM -- "regular" and "adafruit-hat-pwm", which is
# most non-Adafruit boards -- and exits with a message nobody sees because it
# goes to the journal. The symptom is a completely dark panel and a service that
# looks like it started.
#
# Adafruit's own board routes around that pin, which is why a build on one of
# theirs never hits this and a build on anything else does.

say "Onboard sound, which conflicts with the panel"
BOOTCFG=/boot/firmware/config.txt
[[ -f "$BOOTCFG" ]] || BOOTCFG=/boot/config.txt

if [[ ! -f "$BOOTCFG" ]]; then
  warn "no config.txt found; skipping (not a Raspberry Pi?)"
elif grep -qE '^\s*dtparam=audio=off' "$BOOTCFG"; then
  ok "already disabled"
else
  if grep -qE '^\s*dtparam=audio=on' "$BOOTCFG"; then
    step "disable onboard audio" sudo sed -i \
      's/^\s*dtparam=audio=on/dtparam=audio=off/' "$BOOTCFG"
  else
    step "disable onboard audio" bash -c \
      "echo 'dtparam=audio=off' | sudo tee -a '$BOOTCFG' >/dev/null"
  fi
  # Belt and braces: the overlay stops it being created, the blacklist stops it
  # being loaded by anything else.
  printf 'blacklist snd_bcm2835\n' \
    | sudo tee /etc/modprobe.d/blacklist-scoreboard-snd.conf >/dev/null
  LSMOD="$(lsmod 2>/dev/null || true)"
  if grep -q snd_bcm2835 <<<"$LSMOD"; then
    warn "takes effect on the next reboot"
    echo "      until then, PWM mappings (regular, adafruit-hat-pwm) will refuse"
    echo "      to start and the panel stays dark. Run: sudo reboot"
    NEEDS_REBOOT=1
  fi
fi

# --------------------------------------------------------------- 7. mdns

say "mDNS, so the board answers to <name>.local"
step "enable avahi" sudo systemctl enable --now avahi-daemon

# --------------------------------------------------------------- 8. services

say "Services"
sudo tee /etc/systemd/system/scoreboard.service >/dev/null <<UNIT
[Unit]
Description=LED Sports Scoreboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$HERE
ExecStart=$(command -v python3) $HERE/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
ok "scoreboard.service written"
echo "      panel settings come from config.json, no flags needed"

# Self-update settings, when passed. Written before the arming check below
# reads config.json, so a board can be configured and armed in one install run
# rather than needing a trip through the web UI in between.
if [[ -n "${MANIFEST_URL_ARG:-}" || -n "${UPDATE_CHANNEL_ARG:-}" ]]; then
  if python3 - "$HERE/config.json" "${MANIFEST_URL_ARG:-}" "${UPDATE_CHANNEL_ARG:-}" <<'PY'
import json, os, sys
path, url, channel = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as fh:
        data = json.load(fh)
except Exception:
    data = {}
updates = data.setdefault("updates", {})
updates.setdefault("enabled", True)
if url:
    if not url.startswith("https://"):
        raise SystemExit("the manifest URL must be https")
    updates["manifest_url"] = url
if channel:
    if channel not in ("stable", "beta"):
        raise SystemExit("channel must be stable or beta")
    updates["channel"] = channel
updates.setdefault("channel", "stable")
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
os.replace(tmp, path)
PY
  then
    ok "self-update configured: ${UPDATE_CHANNEL_ARG:-stable} channel"
  else
    warn "could not write the self-update settings"
    FAILED+=("self-update settings")
  fi
fi

# The update key goes in /etc, not in the project, and is written once. That
# placement is the whole security model: an update replaces everything under
# $HERE, so a public key stored there could be replaced by the same release it
# is supposed to be verifying. Writing it only when absent means a compromised
# release cannot rotate the key either -- changing it is a physical act.
if [[ -f updates/pubkey.pem ]]; then
  sudo mkdir -p /etc/scoreboard
  if [[ -f /etc/scoreboard/update-key.pem ]]; then
    if sudo cmp -s updates/pubkey.pem /etc/scoreboard/update-key.pem; then
      ok "update key already installed"
    else
      warn "this release carries a DIFFERENT update key than the one installed"
      echo "      Keeping the installed one. A release cannot replace the key that"
      echo "      verifies it; if you rotated keys on purpose, replace it by hand:"
      echo "      sudo cp updates/pubkey.pem /etc/scoreboard/update-key.pem"
    fi
  else
    sudo cp updates/pubkey.pem /etc/scoreboard/update-key.pem
    sudo chmod 644 /etc/scoreboard/update-key.pem
    ok "update key installed to /etc/scoreboard/update-key.pem"
  fi
else
  ok "no updates/pubkey.pem in this release, so self-update stays off"
fi

for unit in scoreboard-hostname.service scoreboard-hostname.path; do
  [[ -f "systemd/$unit" ]] || continue
  sed "s|/home/pi/scoreboard|$HERE|g" "systemd/$unit" \
    | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
step "reload systemd" sudo systemctl daemon-reload
step "enable at boot" sudo systemctl enable scoreboard
step "enable rename watcher" sudo systemctl enable --now scoreboard-hostname.path

# Nightly update check. Only armed when the board has both a key to verify a
# release with and somewhere to look for one; otherwise the timer would wake up
# every night to do nothing.
if [[ -f /etc/scoreboard/update-key.pem ]] \
   && python3 -c "
import json, sys
try:
    data = json.load(open('config.json'))
except Exception:
    data = {}
sys.exit(0 if (data.get('updates') or {}).get('manifest_url') else 1)" 2>/dev/null; then
  for unit in scoreboard-update.service scoreboard-update.timer; do
    [[ -f "systemd/$unit" ]] || continue
    sed "s|/home/pi/scoreboard|$HERE|g" "systemd/$unit" \
      | sudo tee "/etc/systemd/system/$unit" >/dev/null
  done
  sudo systemctl daemon-reload
  step "enable nightly update check" sudo systemctl enable --now scoreboard-update.timer
  NEXT="$(systemctl list-timers scoreboard-update.timer --no-pager 2>/dev/null | sed -n 2p | awk '{print $1, $2, $3}')"
  [[ -n "$NEXT" ]] && echo "      next check: $NEXT"
else
  ok "self-update not armed (set updates.manifest_url in the web UI to arm it)"
fi

# --------------------------------------------------------------- 9. comitup

say "First-boot wifi portal (Comitup)"
if command -v comitup >/dev/null 2>&1; then
  ok "already installed"
elif [[ "$WITH_COMITUP" == "0" ]]; then
  ok "skipped"
elif [[ -n "$WITH_COMITUP" ]] || ask "Install it? Needed only for boards you give away."; then
  if ! systemctl is-active --quiet NetworkManager; then
    warn "needs NetworkManager, which is not active on this image"
    echo "      sudo raspi-config -> Advanced -> Network Config -> NetworkManager"
    FAILED+=("Comitup prerequisite: NetworkManager")
  else
    # Upstream ships a bootstrap package that installs the signing key and the
    # sources entry together. Earlier this hand-rolled both: it fetched a key
    # from a guessed URL and then wrote the sources entry WHETHER OR NOT the key
    # arrived. When the fetch failed, apt was left with a repo it could not
    # verify, which on Trixie makes every `apt-get update` exit non-zero -- so
    # no git, no pip, no Pillow, no matrix library, and a dark panel. One bad
    # key broke ten unrelated steps.
    APT_SRC_DEB="davesteele-comitup-apt-source_1.3_all.deb"
    APT_SRC_URL="https://davesteele.github.io/comitup/deb/$APT_SRC_DEB"

    if step "fetch comitup apt source" \
         curl -fsSL --retry 3 -o "/tmp/$APT_SRC_DEB" "$APT_SRC_URL" \
       && step "install comitup apt source" \
         sudo dpkg -i --force-all "/tmp/$APT_SRC_DEB"; then
      rm -f "/tmp/$APT_SRC_DEB"
      step "apt update" sudo apt-get update
      step "install comitup" sudo apt-get install -y comitup
      if command -v comitup >/dev/null 2>&1; then
        sudo sed -i 's/^# *ap_name:.*/ap_name: scoreboard-<nnn>/' /etc/comitup.conf 2>/dev/null
        ok "access point will appear as scoreboard-<nnn>"
      fi
    else
      warn "could not add the Comitup repository; leaving apt untouched"
      echo "      The board still works, it just cannot open its own wifi"
      echo "      portal, which only matters for a unit you give away."
      echo "      Retry later with: ./install.sh --comitup"
    fi

    # Whatever happened above, apt must still be usable for everything else. A
    # repo entry that cannot be verified fails EVERY update on Trixie, not just
    # its own, so an unusable Comitup source gets removed rather than left to
    # break the next install.
    if ! sudo apt-get update >/dev/null 2>&1; then
      APT_OUT="$(sudo apt-get update 2>&1 || true)"
      if grep -qi 'comitup' <<<"$APT_OUT"; then
        warn "the Comitup repo is breaking apt; removing it"
        sudo rm -f /etc/apt/sources.list.d/comitup.list \
                   /etc/apt/sources.list.d/davesteele-comitup*.list
        sudo apt-get update >/dev/null 2>&1 \
          && ok "apt works again without it" \
          || FAILED+=("apt is still broken after removing the Comitup repo")
      fi
    fi
  fi
else
  ok "skipped"
fi

# --------------------------------------------------------------- 10. start

if [[ $DO_START -eq 1 ]]; then
  say "Starting the board"
  step "start service" sudo systemctl restart scoreboard
  sleep 6
  if systemctl is-active --quiet scoreboard; then
    ok "running"
  else
    warn "service is not running"
    sudo journalctl -u scoreboard -n 12 --no-pager | sed 's/^/      /'
    FAILED+=("service start")
  fi
fi

# --------------------------------------------------------------- 11. verify

say "Verifying"
sudo python3 -c "import flask, requests, PIL" 2>/dev/null \
  && ok "python dependencies" || FAILED+=("python dependencies")
sudo python3 -c "import rgbmatrix" 2>/dev/null \
  && ok "rgbmatrix" || warn "rgbmatrix missing (emulator only, no panel output)"
python3 -m tools.test_scoreboard >/dev/null 2>&1 \
  && ok "self-tests" || FAILED+=("self-tests")
python3 -c "
from data import espn
print('    live data: {} NFL games today'.format(len(espn.scoreboard('nfl'))))
" 2>/dev/null || warn "could not reach ESPN just now (the board retries on its own)"
if [[ $DO_START -eq 1 ]]; then
  PORT="$(python3 -c "import json;print(json.load(open('config.json')).get('web',{}).get('port',8080))" 2>/dev/null || echo 8080)"
  # Give it time to bind. The previous version curled the port in the same
  # breath as starting the service, so on a Pi 3 A+ -- where the process has to
  # import Flask, build the matrix and start the data thread before it listens
  # -- the check lost a race it was never going to win, and reported "web UI"
  # as a failure on a board whose web UI was about to work perfectly. Treating
  # a cold start as a fault is worse than not checking at all: it sends people
  # to debug something that is not broken.
  WEB_OK=0
  for _ in $(seq 1 15); do
    if curl -sf -o /dev/null --max-time 3 "http://localhost:$PORT/"; then
      WEB_OK=1; break
    fi
    sleep 2
  done
  if [[ $WEB_OK -eq 1 ]]; then
    ok "web UI answering on $PORT"
  else
    FAILED+=("web UI")
    warn "still no answer on $PORT after 30s"
    echo "      what the service says:"
    journalctl -u scoreboard -n 8 --no-pager 2>/dev/null | sed 's/^/      /'
  fi
fi

# --------------------------------------------------------------- summary

echo
if ((${#FAILED[@]})); then
  printf '%s%d problem(s):%s\n' "$RED" "${#FAILED[@]}" "$OFF"
  printf '  - %s\n' "${FAILED[@]}"
  printf '\nFix those and run ./install.sh again. It is safe to repeat.\n'
else
  if [[ "${NEEDS_REBOOT:-0}" == "1" ]]; then
    printf '\n    %s!%s %sReboot needed.%s The onboard sound driver is still loaded\n' \
      "$RED" "$OFF" "$BOLD" "$OFF"
    echo "      and blocks the panel on PWM mappings. Run: sudo reboot"
  fi
  printf '%sInstall complete.%s\n' "$GRN" "$OFF"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST="$(hostname 2>/dev/null)"
cat <<INFO

  Set it up:  http://${HOST:-raspberrypi}.local:8080
              http://${IP:-<pi-address>}:8080

  Manage it:  ./scoreboard status
              ./scoreboard logs
              ./scoreboard doctor

INFO
exit $(( ${#FAILED[@]} > 0 ))
