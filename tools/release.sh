#!/usr/bin/env bash
# Build, sign and publish a release the boards will pick up on their own.
#
# Runs on your Mac, in a checkout of the repo. What it does, in order:
#
#   1. builds the zip from the current commit
#   2. hashes it
#   3. writes that version into one channel of updates/manifest.json
#   4. signs the manifest with your private key
#   5. uploads the zip as a GitHub release asset and commits the manifest
#
# The signature is the part that matters and the part that is easy to skip.
# Boards refuse a manifest they cannot verify, so an unsigned or wrongly signed
# release simply does not install -- which is the correct failure, and also
# means you cannot accidentally ship without signing.
#
#   ./tools/release.sh beta                  publish the current commit to beta
#   ./tools/release.sh stable --promote      copy whatever beta is on to stable
#   ./tools/release.sh beta --dry-run        build and sign, publish nothing
#   ./tools/release.sh beta --skip-tests     publish without running them here
#
# The tests run twice by design: here, before anything is published, and again
# on each board against the downloaded release before it replaces a working
# install. --skip-tests drops the first of those, not the second, so a broken
# release still cannot land on a board -- it just wastes a download finding out.
#
# First time only:
#
#   ./tools/release.sh --init-key ~/.scoreboard/release-key.pem
#
#   python3 -m venv ~/.scoreboard/venv
#   ~/.scoreboard/venv/bin/pip install -q requests Pillow Flask pytz tzlocal
#
# The venv is what lets this machine run the test suite. It is picked up
# automatically if it exists; without it, publishing still works but you have to
# pass --skip-tests and let the boards do the checking.
#
# Keep that private key off the boards, out of the repo, and backed up
# somewhere you will still have it in two years. Losing it means no board can
# be updated again without physically visiting it; leaking it means whoever has
# it can run code on all four.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

KEY="${SCOREBOARD_RELEASE_KEY:-$HOME/.scoreboard/release-key.pem}"
MANIFEST="updates/manifest.json"
PUBKEY="updates/pubkey.pem"
DIST="${TMPDIR:-/tmp}/scoreboard-dist"

BOLD=$'\033[1m'; RED=$'\033[1;31m'; GRN=$'\033[1;32m'; OFF=$'\033[0m'
ok()   { printf '  %s✓%s %s\n' "$GRN" "$OFF" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$RED" "$OFF" "$*" >&2; }
head_() { printf '\n%s%s%s\n' "$BOLD" "$*" "$OFF"; }
die()  { bad "$*"; exit 1; }

# macOS and Linux disagree on both of these, and this script runs on a Mac.
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# ---------------------------------------------------------------- key setup

if [[ "${1:-}" == "--init-key" ]]; then
  DEST="${2:-$KEY}"
  [[ -f "$DEST" ]] && die "$DEST already exists; refusing to overwrite a signing key"
  mkdir -p "$(dirname "$DEST")"
  ( umask 077; openssl genrsa -out "$DEST" 4096 )
  openssl rsa -in "$DEST" -pubout -out "$PUBKEY"
  head_ "Signing key created"
  ok "private key: $DEST  (never leaves this machine)"
  ok "public key:  $PUBKEY  (ships in the release, installed to /etc on each board)"
  cat <<'NOTE'

  Back the private key up now, somewhere you will still have in two years.
  A password manager's secure-note field is fine. If you lose it, no board can
  be updated again without physically getting to it.

  Commit the public key, then build the gift cards from a release that contains
  it: install.sh copies it to /etc/scoreboard/update-key.pem on first install
  and never replaces it afterwards, which is what stops a compromised release
  from also replacing the key that would have caught it.

NOTE
  exit 0
fi

CHANNEL="${1:-}"
[[ "$CHANNEL" == "beta" || "$CHANNEL" == "stable" ]] \
  || die "Usage: $0 <beta|stable> [--promote] [--dry-run]   (or --init-key)"

DRY=0; PROMOTE=0; SKIP_TESTS=0
for arg in "${@:2}"; do
  case "$arg" in
    --dry-run)    DRY=1 ;;
    --promote)    PROMOTE=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    *) die "unknown option: $arg" ;;
  esac
done

# Which python runs the test suite. The board has its dependencies installed by
# install.sh; a Mac used only for publishing has no reason to, and a missing
# library there is not a failing test. A venv keeps them out of the system
# python, which recent macOS and Homebrew both refuse to let pip touch anyway.
TEST_PY="python3"
[[ -x "$HOME/.scoreboard/venv/bin/python3" ]] && TEST_PY="$HOME/.scoreboard/venv/bin/python3"

[[ -f "$KEY" ]] || die "no signing key at $KEY. Run: $0 --init-key"
[[ -f "$MANIFEST" ]] || printf '{"schema":1,"channels":{}}\n' > "$MANIFEST"

REPO="$(git config --get remote.origin.url 2>/dev/null \
  | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
if [[ -z "$REPO" ]]; then
  bad "this is not a git checkout with a GitHub remote"
  cat <<'SETUP' >&2

  Releases are published from a clone of your own repo, because that is where
  the boards look for the manifest. One-time setup:

      brew install gh
      gh auth login

      cd ~/Downloads
      unzip -o led-scoreboard-v*.zip
      cd scoreboard
      git init -b main
      git add -A
      git commit -m "scoreboard: first commit"
      gh repo create led-scoreboard --public --source=. --remote=origin --push

  Public because the boards then need no credentials at all, and because
  upstream is GPLv3, so the source has to be available to anyone you hand a
  board to anyway. Then run this script again from inside that clone.

SETUP
  exit 1
fi

# ---------------------------------------------------------------- promote

if [[ $PROMOTE -eq 1 ]]; then
  head_ "Promoting beta to stable"
  python3 - "$MANIFEST" <<'PY'
import json, sys, time
path = sys.argv[1]
with open(path) as fh:
    data = json.load(fh)
ch = data.setdefault("channels", {})
beta = ch.get("beta")
if not beta:
    raise SystemExit("nothing on beta to promote")
if ch.get("stable", {}).get("version") == beta.get("version"):
    raise SystemExit("stable is already on {}".format(beta["version"]))
ch["stable"] = dict(beta)
ch["stable"]["promoted"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
data["published"] = ch["stable"]["promoted"]
with open(path, "w") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
print("stable -> {}".format(beta["version"]))
PY
  VERSION="$(python3 -c "import json;print(json.load(open('$MANIFEST'))['channels']['stable']['version'])")"
  ok "manifest updated"
else
  # ------------------------------------------------------------- build
  VERSION="$(grep -m1 '^VERSION' main.py | sed 's/.*"\(.*\)".*/\1/')"
  [[ -n "$VERSION" ]] || die "could not read VERSION from main.py"
  head_ "Building v$VERSION for $CHANNEL"

  # A release has to correspond to a commit, or "which code is on that board"
  # becomes unanswerable the first time something goes wrong in someone else's
  # house. Refuse to build from a dirty tree rather than shipping a version
  # number that matches no commit anywhere.
  if [[ -n "$(git status --porcelain -- . ':(exclude)updates' 2>/dev/null)" ]]; then
    bad "the working tree has uncommitted changes"
    git status --short -- . ':(exclude)updates' | sed 's/^/      /' >&2
    cat <<'SETUP' >&2

  A published release should be a commit you can go back to. Commit first:

      git add -A && git commit -m "scoreboard v<version>"

SETUP
    exit 1
  fi

  # A release that cannot pass its own tests is one the boards would download
  # and reject anyway. Find out here, before anything is published.
  #
  # "Could not run" and "ran and failed" are different answers and used to get
  # the same message. A publishing machine has no reason to have the board's
  # runtime installed, so a missing library was reported as a broken release,
  # which is both wrong and unhelpful: the fix is one command and the message
  # did not say so.
  rm -rf "$DIST"; mkdir -p "$DIST"
  if [[ $SKIP_TESTS -eq 1 ]]; then
    bad "skipping the self-tests at your request"
    echo "      Each board still runs them against the download before it"
    echo "      installs anything, so this risks a wasted round trip rather"
    echo "      than a broken board."
  elif "$TEST_PY" -m tools.test_scoreboard >"$DIST/tests.log" 2>&1; then
    ok "self-tests pass"
  elif grep -q "ModuleNotFoundError" "$DIST/tests.log"; then
    MISSING="$(grep -o "No module named '[^']*'" "$DIST/tests.log" \
      | head -1 | sed "s/.*'\(.*\)'/\1/")"
    bad "the tests could not run here: no module named '${MISSING:-?}'"
    cat <<'SETUP' >&2

  Not a broken release -- this Mac just does not have the board's runtime.
  Set up a virtual environment once and every future release will use it:

      python3 -m venv ~/.scoreboard/venv
      ~/.scoreboard/venv/bin/pip install -q requests Pillow Flask pytz tzlocal

  A venv rather than a plain pip install because recent macOS and Homebrew
  both refuse to let pip write to the system python.

  Or publish without testing here, and let the boards do it:

      ./tools/release.sh CHANNEL --skip-tests

SETUP
    exit 1
  else
    tail -20 "$DIST/tests.log" >&2
    die "the self-tests ran and failed; not publishing"
  fi

  ZIPNAME="led-scoreboard-v$VERSION.zip"

  # Built with git archive for three reasons that all matter here:
  #
  #   --prefix pins the top-level folder to `scoreboard` whatever this checkout
  #   is called. Everything downstream depends on that name -- the card-prep
  #   script, ./scoreboard update, and the self-updater, which unpacks beside
  #   the install and would otherwise quietly create a second copy under a
  #   different name and report success. A clone named after the GitHub repo
  #   instead of the project is the obvious way to trip over that.
  #
  #   It ships tracked files only, so .gitignore is the single definition of
  #   what is source: no stray venv, no other board's config.json, no cached
  #   logos, and no need to keep an exclude list here in step with one there.
  #
  #   And it builds from the commit, so the archive is reproducible from the
  #   tag rather than from whatever happened to be on this laptop.
  git archive --format=zip --prefix=scoreboard/ -o "$DIST/$ZIPNAME" HEAD \
    || die "git archive failed"

  # Listed once into a variable rather than piped into grep twice, and that is
  # not a style choice. `set -o pipefail` is on, and `grep -q` exits the moment
  # it matches, which closes the pipe under unzip and can make unzip die of
  # SIGPIPE. The pipeline then reports unzip's failure, so the check fails
  # *because* the pattern was found -- intermittently, depending on whether
  # unzip had finished writing. It cost an hour to find once; it does not get
  # to happen twice.
  LISTING="$(unzip -l "$DIST/$ZIPNAME")" || die "cannot read the archive back"
  grep -q 'scoreboard/main.py' <<<"$LISTING" \
    || die "the archive does not contain scoreboard/main.py"
  grep -q 'scoreboard/updates/pubkey.pem' <<<"$LISTING" \
    || die "the archive has no updates/pubkey.pem, so a board installed from it could never verify a release. Commit the public key first."
  ok "$ZIPNAME  ($(du -h "$DIST/$ZIPNAME" | awk '{print $1}'))"

  SUM="$(sha256 "$DIST/$ZIPNAME")"
  URL="https://github.com/$REPO/releases/download/v$VERSION/$ZIPNAME"
  NOTES="${RELEASE_NOTES:-v$VERSION}"

  python3 - "$MANIFEST" "$CHANNEL" "$VERSION" "$URL" "$SUM" "$NOTES" <<'PY'
import json, sys, time
path, channel, version, url, sha, notes = sys.argv[1:7]
with open(path) as fh:
    data = json.load(fh)
data.setdefault("schema", 1)
stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
data.setdefault("channels", {})[channel] = {
    "version": version, "url": url, "sha256": sha,
    "notes": notes, "published": stamp,
}
data["published"] = stamp
with open(path, "w") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY
  ok "manifest points $CHANNEL at $VERSION"
fi

# ---------------------------------------------------------------- sign

openssl dgst -sha256 -sign "$KEY" -out updates/manifest.sig "$MANIFEST"
openssl dgst -sha256 -verify "$PUBKEY" -signature updates/manifest.sig "$MANIFEST" >/dev/null \
  || die "signed the manifest but cannot verify it with $PUBKEY -- the key pair does not match"
ok "manifest signed and the signature verifies against the shipped public key"
# Worth knowing rather than discovering: the copy of updates/manifest.json
# inside the archive is one release behind, because the manifest records this
# archive's hash and so can only be written after it exists. Harmless -- boards
# read the manifest from the URL, never from their own install.

if [[ $DRY -eq 1 ]]; then
  head_ "Dry run"
  ok "nothing uploaded, nothing committed"
  echo "  manifest: $MANIFEST"
  echo "  signature: updates/manifest.sig"
  exit 0
fi

# ---------------------------------------------------------------- publish

head_ "Publishing"

# Order matters here, and it is the opposite of the obvious one. The manifest is
# the switch that turns a release on: the moment it is pushed, every board on
# that channel will fetch whatever URL it names. So the asset goes up first, the
# URL is confirmed to resolve, and only then is the manifest pushed. Done the
# other way round, a failed or forgotten upload leaves four boards downloading a
# 404 every night -- harmless, because they verify before installing, but it
# fails in the one place nobody is looking.

if [[ $PROMOTE -eq 0 ]]; then
  if command -v gh >/dev/null 2>&1; then
    if gh release view "v$VERSION" >/dev/null 2>&1; then
      gh release upload "v$VERSION" "$DIST/$ZIPNAME" --clobber
    else
      gh release create "v$VERSION" "$DIST/$ZIPNAME" \
        --title "v$VERSION" --notes "$NOTES"
    fi
    ok "release asset uploaded"
  else
    head_ "gh is not installed, so upload the zip by hand"
    cat <<MANUAL

  The signed manifest is ready on disk but NOT pushed, because pushing it is
  what tells the boards to go and get a file that does not exist yet.

  1. Open:  https://github.com/$REPO/releases/new
  2. Tag:   v$VERSION        (create it on this page)
  3. Attach this file:
       $DIST/$ZIPNAME
  4. Publish the release, then run this command again. It will find the asset
     and push the manifest.

  Or install gh and skip all of that next time:
       brew install gh && gh auth login

MANUAL
    exit 1
  fi
fi

# Confirm the thing the manifest points at is actually there. Catches a failed
# upload, a wrong tag, and a private repo that boards could never read.
ASSET_URL="$(python3 -c "import json;print(json.load(open('$MANIFEST'))['channels']['$CHANNEL']['url'])")"
if curl -fsIL --max-time 30 "$ASSET_URL" >/dev/null 2>&1; then
  ok "the release asset is reachable without credentials"
else
  bad "cannot reach $ASSET_URL"
  cat <<'NOTE' >&2

  The manifest was NOT pushed, so no board has been told to look for this.
  Either the upload has not finished, the tag name does not match, or the repo
  is private -- boards carry no credentials, so a private repo cannot work.
  Fix it and run this again; nothing is lost.

NOTE
  exit 1
fi

git add "$MANIFEST" updates/manifest.sig "$PUBKEY" 2>/dev/null || true
git commit -qm "release: $CHANNEL -> v$VERSION" || echo "  (nothing to commit)"
git push -q
ok "manifest pushed -- boards on $CHANNEL will pick this up"

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
MANIFEST_URL="https://raw.githubusercontent.com/$REPO/$BRANCH/updates/manifest.json"

cat <<INFO

  ${BOLD}$CHANNEL is now on v$VERSION${OFF}

  Put this in each board's web page, under Updates -> Release manifest:

      $MANIFEST_URL

  Boards on this channel pick it up at their next nightly check, or now with:
      ./scoreboard selfupdate --force

INFO
if [[ "$CHANNEL" == "beta" ]]; then
  cat <<'INFO'
  Let your own board run it for a day, then send it to the gifts:
      ./tools/release.sh stable --promote

INFO
fi
