#!/usr/bin/env bash
# Pull an update from the release channel, install it, and undo it if it breaks.
#
# The whole point of this script is that it runs on a board in a house you
# cannot get into. That single fact drives every decision below.
#
#   * Outbound HTTPS only. Nothing listens, nothing is port-forwarded, nothing
#     is configured on their router. A board behind any home NAT can be
#     updated, including one behind carrier-grade NAT where inbound is not an
#     option at all.
#
#   * The manifest is signed, and the signature is checked before anything is
#     downloaded. This is a remote code execution channel into somebody else's
#     home network, and HTTPS alone only proves you reached GitHub -- not that
#     what GitHub served is yours. If the account is compromised, or a repo is
#     renamed and somebody claims the abandoned name, the signature is what
#     stops that becoming four compromised houses. The public key lives in
#     /etc, outside the tree an update replaces, so an attacker who can publish
#     a release still cannot publish the key used to verify it.
#
#   * A failed update rolls itself back, and that matters more in practice than
#     the security does. The realistic disaster is not an attacker, it is
#     shipping a bad release to three boards you cannot reach. So: the new tree
#     is tested in a staging directory while the old one keeps running, the
#     previous copy is kept, the service has to come up and report the expected
#     version on its own port, and any failure restores what was working.
#
#   * A version that failed here is remembered and not retried. Without that, a
#     bad release means every board installs it, fails, rolls back, and does
#     the same thing again tomorrow, forever.
#
# Run by scoreboard-update.timer overnight, or by hand:
#
#     ./scoreboard selfupdate              check, install if newer
#     ./scoreboard selfupdate --dry-run    say what it would do
#     ./scoreboard selfupdate --force      ignore the quiet window and the
#                                          failed-version list

set -uo pipefail

# ---------------------------------------------------------------- re-exec
#
# This script is about to replace the directory it lives in. bash reads a
# script incrementally, so overwriting the file mid-run makes it resume at a
# byte offset into different content and execute whatever is there. Copy to a
# scratch path and carry on from the copy.
if [[ "${SELFUPDATE_DETACHED:-0}" != "1" ]]; then
  COPY="$(mktemp -t scoreboard-selfupdate.XXXXXX)" || exit 1
  cp "${BASH_SOURCE[0]}" "$COPY" || exit 1
  chmod +x "$COPY"
  export SELFUPDATE_DETACHED=1
  export SELFUPDATE_ORIGIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  exec bash "$COPY" "$@"
fi

export HERE="${SELFUPDATE_ORIGIN:?}"
cd "$HERE" || exit 1

STATE="$HERE/state"
BACKUP="$(dirname "$HERE")/scoreboard-previous"
STAGING="$(dirname "$HERE")/scoreboard-staging"
# Overridable only so the test suite can point at a throwaway keypair. Not a
# weakness worth worrying about: anyone who can set this variable on the update
# unit can already run code as root on the board.
KEY="${SELFUPDATE_KEY:-/etc/scoreboard/update-key.pem}"
LOG="$STATE/update.log"
FAILED_LIST="$STATE/update-failed-versions"

DRY=0; FORCE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --force)   FORCE=1 ;;
  esac
done

mkdir -p "$STATE"

say()  { printf '%s\n' "$*"; printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG" 2>/dev/null; }
record() {
  # One small file the web UI and ./scoreboard status both read, so "is this
  # board up to date" is answerable without ssh.
  python3 - "$1" "$2" <<'PY' 2>/dev/null || true
import json, os, sys, time
path = os.path.join(os.environ["HERE"], "state", "update-status.json")
try:
    with open(path) as fh:
        data = json.load(fh)
except Exception:
    data = {}
data.update({"result": sys.argv[1], "detail": sys.argv[2],
             "checked_at": time.time()})
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh)
os.replace(tmp, path)
PY
}
note() {
  python3 - "$1" "$2" <<'PY' 2>/dev/null || true
import json, os, sys, time
path = os.path.join(os.environ["HERE"], "state", "update-status.json")
try:
    with open(path) as fh:
        data = json.load(fh)
except Exception:
    data = {}
data[sys.argv[1]] = sys.argv[2]
data["checked_at"] = time.time()
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh)
os.replace(tmp, path)
PY
}
die()  { say "ERROR: $*"; record "error" "$*"; exit 1; }
skip() { say "$*"; record "skipped" "$*"; exit 0; }

setting() {
  python3 - "$1" "$2" <<'PY' 2>/dev/null || echo "$2"
import json, os, sys
key, default = sys.argv[1], sys.argv[2]
try:
    with open(os.path.join(os.environ["HERE"], "config.json")) as fh:
        data = json.load(fh)
except Exception:
    print(default); raise SystemExit
node = data
for part in key.split("."):
    if not isinstance(node, dict) or part not in node:
        print(default); raise SystemExit
    node = node[part]
if isinstance(node, bool):
    print("1" if node else "0")
elif node is None or node == "":
    print(default)
else:
    print(node)
PY
}

version_in() { grep -m1 '^VERSION' "$1/main.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/'; }

# Is $1 strictly newer than $2?
newer_than() {
  [[ "$1" != "$2" ]] \
    && [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" == "$1" ]]
}

cleanup() { rm -rf "$TMP" "$STAGING" 2>/dev/null; rm -f "$0" 2>/dev/null; }

# ---------------------------------------------------------------- preflight

say "--- update check ---"

[[ "$(setting updates.enabled 1)" == "1" ]] || skip "updates are switched off in settings"

# The unpack puts a directory called `scoreboard` next to this one, so an
# install under any other name would quietly grow a second copy rather than
# updating this one.
[[ "$(basename "$HERE")" == "scoreboard" ]] \
  || die "this install lives in $(basename "$HERE"), not 'scoreboard'; self-update expects the standard layout"

CHANNEL="$(setting updates.channel stable)"
BASE="$(setting updates.manifest_url "")"
[[ -n "$BASE" ]] || skip "no manifest URL configured"
[[ "$BASE" == https://* ]] || die "the manifest URL must be https, got: $BASE"

for tool in openssl curl unzip sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is missing, cannot update safely"
done
[[ -f "$KEY" ]] \
  || die "no update key at $KEY -- install.sh puts it there. This board cannot verify a release, so it will not install one."

# The clock has to be right before HTTPS or a quiet window mean anything: a Pi
# has no battery-backed clock, so a board unplugged since last season comes up
# with a stale date.
if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" != "yes" ]]; then
  for _ in $(seq 1 30); do
    sleep 2
    [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]] && break
  done
fi
[[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]] \
  || skip "clock not synchronised yet, will try again next time"

TMP="$(mktemp -d)" || die "no temp space"
trap cleanup EXIT

# ---------------------------------------------------------------- manifest

MANIFEST="$TMP/manifest.json"
curl -fsS --max-time 30 --retry 2 -o "$MANIFEST" "$BASE" \
  || die "could not fetch the manifest from $BASE"
curl -fsS --max-time 30 --retry 2 -o "$TMP/manifest.sig" "${BASE%.json}.sig" \
  || die "could not fetch the manifest signature"

openssl dgst -sha256 -verify "$KEY" -signature "$TMP/manifest.sig" "$MANIFEST" >/dev/null 2>&1 \
  || die "manifest signature does NOT verify. Refusing it: either the release was not signed with this board's key, or something is serving content that is not ours."
say "manifest signature verified"

read -r WANT URL SUM NOTES < <(python3 - "$MANIFEST" "$CHANNEL" <<'PY'
import json, sys
with open(sys.argv[1]) as fh:
    data = json.load(fh)
ch = (data.get("channels") or {}).get(sys.argv[2]) or {}
url = str(ch.get("url", ""))
# A signed manifest that points somewhere else is still a manifest telling the
# board to run code from somewhere else, so the scheme is checked here too.
if not url.startswith("https://"):
    url = ""
print(str(ch.get("version", "")) or "-", url or "-",
      str(ch.get("sha256", "")) or "-",
      (str(ch.get("notes", "")) or "-").replace(" ", "_"))
PY
)

[[ "$WANT" != "-" && "$URL" != "-" && "$SUM" != "-" ]] \
  || skip "channel '$CHANNEL' has no usable release in the manifest"

HAVE="$(version_in "$HERE")"
note installed "$HAVE"
note channel "$CHANNEL"
note available "$WANT"

newer_than "$WANT" "$HAVE" \
  || skip "up to date on $CHANNEL (running $HAVE, offered $WANT)"

if [[ $FORCE -eq 0 ]] && grep -qxF "$WANT" "$FAILED_LIST" 2>/dev/null; then
  skip "$WANT failed on this board before and was rolled back; not retrying it (--force overrides)"
fi

say "update available: $HAVE -> $WANT  ($NOTES)"

PORT="$(setting web.port 8080)"

if [[ $FORCE -eq 0 ]]; then
  # Never mid-game. A board that reboots during a drive is a worse experience
  # than a board that is a day out of date.
  LIVE="$(curl -fsS --max-time 5 "http://localhost:$PORT/api/status" 2>/dev/null \
    | python3 -c 'import json,sys;print((json.load(sys.stdin).get("counts") or {}).get("live",0))' 2>/dev/null || echo 0)"
  [[ "${LIVE:-0}" -eq 0 ]] \
    || skip "$LIVE game(s) live right now, leaving it until the next quiet check"
fi

if [[ $DRY -eq 1 ]]; then
  say "dry run: would install $WANT from $URL"
  record "dry-run" "would install $WANT"
  exit 0
fi

# ---------------------------------------------------------------- download

ZIP="$TMP/release.zip"
curl -fsSL --max-time 600 --retry 2 -o "$ZIP" "$URL" || die "could not download $URL"

GOT="$(sha256sum "$ZIP" | awk '{print $1}')"
[[ "$GOT" == "$SUM" ]] \
  || die "checksum mismatch (manifest said $SUM, download was $GOT)"
unzip -tq "$ZIP" >/dev/null 2>&1 || die "the downloaded archive is damaged"
say "download verified"

# ---------------------------------------------------------------- stage
#
# Unpack and test beside the running install rather than on top of it. The
# panel keeps showing scores through the slowest part of the job -- the test
# suite takes minutes on a Pi 3 -- and a release that cannot even pass its own
# tests never reaches the live tree at all.

rm -rf "$STAGING"
mkdir -p "$STAGING"
unzip -oq "$ZIP" -d "$STAGING" || die "unzip failed"
NEW="$STAGING/scoreboard"
[[ -f "$NEW/main.py" ]] || die "the archive does not look like a scoreboard release"

STAGED="$(version_in "$NEW")"
[[ "$STAGED" == "$WANT" ]] \
  || die "the archive reports version $STAGED, the manifest promised $WANT"

say "running the release's own tests before touching the live copy"
if ! ( cd "$NEW" && timeout 900 python3 -m tools.test_scoreboard >"$TMP/tests.log" 2>&1 ); then
  tail -25 "$TMP/tests.log" >>"$LOG" 2>/dev/null
  echo "$WANT" >>"$FAILED_LIST"
  die "$WANT fails its own self-tests; not installing it. Last lines are in $LOG"
fi
say "self-tests pass"

# ---------------------------------------------------------------- install

OWNER="$(stat -c %U "$HERE" 2>/dev/null || echo root)"

say "backing up the working copy"
rm -rf "$BACKUP"
cp -a "$HERE" "$BACKUP" || die "could not back up the current install"

fix_permissions() {
  local root="$1"
  sudo chown -R "$OWNER" "$root" 2>/dev/null
  sudo chgrp -R daemon "$root" 2>/dev/null
  sudo chmod 775 "$root" "$root/state" "$root/logo_cache" 2>/dev/null
  sudo chmod 664 "$root/config.json" 2>/dev/null
  chmod +x "$root/install.sh" "$root/scoreboard" "$root/prepare-for-gift.sh" \
    "$root/tools/selfupdate.sh" 2>/dev/null
}

rollback() {
  say "ROLLING BACK to $HAVE: $1"
  sudo systemctl stop scoreboard 2>/dev/null
  # Keep this board's own history across the restore: the log and the
  # failed-version list are the only record of why it rolled back, and they
  # live in the tree that is about to be replaced.
  local keep="$TMP/state-keep"
  rm -rf "$keep"; mkdir -p "$keep"
  cp -a "$HERE/state/." "$keep/" 2>/dev/null
  rm -rf "$HERE"
  mkdir -p "$HERE"
  cp -a "$BACKUP/." "$HERE/" || say "restore copy reported errors"
  cp -a "$keep/." "$HERE/state/" 2>/dev/null
  echo "$WANT" >>"$HERE/state/update-failed-versions"
  fix_permissions "$HERE"
  sudo systemctl start scoreboard 2>/dev/null
  record "rolled-back" "$WANT failed here ($1). Restored $HAVE."
  say "rolled back; running $HAVE again"
  exit 1
}

# state/, logo_cache/ and config.json are not in the zip, so settings and this
# board's history survive the unpack. Copied across explicitly anyway, because
# "a release must never overwrite somebody's teams" should not depend on a
# packaging detail staying true.
cp -a "$HERE/config.json" "$NEW/config.json" 2>/dev/null
mkdir -p "$NEW/state" "$NEW/logo_cache"
cp -a "$HERE/state/." "$NEW/state/" 2>/dev/null

sudo systemctl stop scoreboard 2>/dev/null
rm -rf "$HERE"
mv "$NEW" "$HERE" || rollback "could not move the new tree into place"
# Logo processing changes between releases and a stale cache hides the fix.
rm -rf "$HERE"/logo_cache/* 2>/dev/null
fix_permissions "$HERE"

sudo systemctl start scoreboard || rollback "the service would not start"

# ---------------------------------------------------------------- verify

HEALTHY=0
for _ in $(seq 1 20); do
  sleep 3
  systemctl is-active --quiet scoreboard || continue
  REPORTED="$(curl -fsS --max-time 4 "http://localhost:$PORT/api/status" 2>/dev/null \
    | python3 -c 'import json,sys;print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true)"
  [[ "$REPORTED" == "$WANT" ]] && { HEALTHY=1; break; }
done
[[ $HEALTHY -eq 1 ]] \
  || rollback "the service never reported version $WANT on port $PORT"

# A crash loop takes a few seconds to become visible, so look once more before
# declaring victory.
sleep 15
systemctl is-active --quiet scoreboard || rollback "the service died shortly after starting"

say "updated to $WANT and healthy"
note installed "$WANT"
record "updated" "installed $WANT"
rm -rf "$BACKUP"
exit 0
