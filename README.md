# LED Sports Scoreboard

An enhanced fork of [mikemountain/nfl-led-scoreboard](https://github.com/mikemountain/nfl-led-scoreboard)
for a 64x32 RGB LED matrix on a Raspberry Pi.

The original shows NFL games. This adds college football (Top 25 plus your
teams), any other league ESPN carries, stats and standings screens, a
priority-based rotation, and a phone-friendly config page so you never have to
SSH in to change a favorite team.

![all screens](out/contact_sheet.png)

## What's different from upstream

| | upstream | this |
|---|---|---|
| Leagues | NFL | NFL, CFB, NBA, CBB, NHL, MLB, WNBA, EPL, UCL, MLS |
| College | none | Top 25 by AP/CFP poll, ranked-only filter, rank badges |
| Rotation | round-robin | favorites first, live before pregame, ranked before unranked, capped slate |
| Network | blocking HTTP inside the render loop | background thread, render loop never does I/O |
| Logos | ~90 bundled NFL PNGs | those, plus any team fetched from ESPN and cached at panel size |
| Non-game screens | none | Top 25, standings, game leaders, team card, ticker, clock, weather |
| Config | edit `config.json`, restart | web UI on port 8080, applies live |
| Failure mode | crash or freeze | last good data stays up, stale indicator in the corner |
| Testing | on the panel, in season | emulator, fixtures, contact sheet, `tools.test_scoreboard` |

## Install on the Pi

One command, from a zip or straight from a repo:

```bash
unzip led-scoreboard.zip && cd scoreboard && ./install.sh
```

```bash
# or, unattended, which is what you want when building the third unit
./install.sh --yes --repo https://github.com/you/scoreboard.git
```

It installs dependencies (for root, which is what runs the board), builds
hzeller's `rpi-rgb-led-matrix` with the Python bindings, writes `config.json`,
sets the permissions the privilege drop needs, enables mDNS, installs the
systemd units, optionally installs Comitup, starts the board, and verifies the
result. It ends with a URL or a list of exactly what failed, and it is safe to
run repeatedly.

No panel flags needed: the board detects itself and reads `config.json`.

Flags: `--yes` for no prompts, `--no-comitup`, `--no-start`, `--repo <url>`,
`--zip <path>`, `--dir <path>`.

## Day-to-day

```bash
./scoreboard status     # running? what is it showing? where do I open it?
./scoreboard doctor     # diagnose the usual failures, with the fix for each
./scoreboard logs       # follow the log
./scoreboard run        # foreground, for debugging
./scoreboard update     # unpack a new zip, keep config, clear logo cache, restart
./scoreboard prepare    # reset for handing to someone else
```

`doctor` is the one worth knowing. It checks the things that actually break:
whether **root** can import flask and rgbmatrix (installing as your user is the
classic mistake, and the only symptom is the web UI silently not starting),
whether `config.json` survives the privilege drop to `daemon`, whether the
timezone is still UTC, whether ESPN is reachable, whether the web UI answers,
and whether the service is restart-looping. Each failure prints the command
that fixes it.

## Develop without a Pi

```bash
pip3 install -r requirements.txt
python3 main.py --backend emulator          # panel in a browser window
python3 -m tools.screenshots --scale 6      # every screen -> out/contact_sheet.png
python3 -m tools.test_scoreboard            # parser, config, rotation, render
```

The screenshot tool runs off fixtures, so layouts can be checked in July with
no network and no games in progress. To work from real data instead, capture it
once and replay it:

```bash
python3 -m tools.capture ncaaf nfl
SCOREBOARD_FIXTURES=fixtures python3 main.py --backend emulator
```

Capturing during an actual red-zone drive and replaying it later is the only
sane way to iterate on the live-game layout out of season.

## Building a card from scratch

A new card cannot be flashed and handed straight to someone. Comitup is what
lets them join their own wifi, but Comitup has to be installed first, and
installing it needs a network. So every unit gets built on **your** wifi, then
reset for handoff. Budget about an hour for the first one and fifteen minutes
for each clone.

**1. Flash with Raspberry Pi Imager.** Choose Raspberry Pi OS Lite, and pick
the bitness by how much RAM the board has rather than by its model name:

| Board | RAM | Choose |
| --- | --- | --- |
| Pi Zero W (original) | 512 MB | 32-bit, and it is the only option: ARMv6 |
| Pi Zero 2 W | 512 MB | 32-bit |
| Pi 3 A+ | 512 MB | 32-bit |
| Pi 3 B+ | 1 GB | 64-bit |
| Pi 4 / Pi 5 | 2 GB+ | 64-bit |

The 3 A+ and the Zero 2 W are 64-bit chips, so a 64-bit image boots and runs.
It just costs 100 to 200 MB more at idle, which is a real fraction of 512 MB
and buys nothing here: the board is a Python process drawing to a 64x32 frame,
not something that benefits from wider registers.

Before writing, open the gear icon / OS Customisation and set:

- Hostname: `scoreboard`
- Username and password (current Pi OS has no default `pi` account any more)
- Wireless LAN: **your** network, and set the country correctly or wifi stays off
- Locale and timezone
- Services tab: enable SSH with password authentication

Then check whether Imager actually wrote them:

```bash
ls -a /Volumes/bootfs | grep -iE 'custom.toml|firstrun|wpa_supp|userconf'
```

Empty output is common and is not a mistake on your part. Imager has a known
intermittent bug where the settings are entered, the write reports success, and
nothing reaches the card, traced to the files not being flushed before eject.
It was reported against 2.0.6 and fixed later. Update Imager, but do not depend
on it: `prepare-sd-card.sh` can write the same `custom.toml` itself with
`--wifi`/`--wifi-pass` and `--user`/`--user-pass`, which is one deterministic
write instead of a race.

This is the whole reason you never need to SSH into an unconfigured board:
Imager writes those settings onto the card before first boot.

**2. Boot it on your network and find it.**

```bash
ping scoreboard.local
ssh <your-user>@scoreboard.local
```

If `.local` does not resolve, check your router's client list for the IP.

**3. Install.** There are two ways in. Prefer the first.

*From the SD card, with no terminal on the Pi at all.* Right after Imager
finishes, with the card still in the Mac:

```bash
cd ~/Downloads
unzip -o led-scoreboard-v2.12.zip 'scoreboard/boot/*'
chmod +x scoreboard/boot/prepare-sd-card.sh
scoreboard/boot/prepare-sd-card.sh ~/Downloads/led-scoreboard-v2.12.zip \
  --name "Living Room Scoreboard"
```

That drops the payload on the card's FAT partition and hooks it into the first
boot. Put the card in the Pi and power it: it boots once to arm the installer,
reboots, and installs itself on the second boot. No scp, no ssh, no password
typed into a terminal. Progress is written to `scoreboard-install.log` on the
card, so a failure can be read by putting the card back in the Mac.
[`boot/README.md`](boot/README.md) has the details.

*Or over ssh,* if the Pi is already up and you would rather watch:

```bash
# from your Mac
scp led-scoreboard-v2.12.zip <user>@scoreboard.local:~
# on the Pi
unzip led-scoreboard-v2.12.zip && cd scoreboard
chmod +x install.sh && ./install.sh
```

Say yes to Comitup when it asks. On current Pi OS, NetworkManager is already
the default, which Comitup requires.

**4. Test it properly.** Let it run, check the panel, tune `pwm_bits` and
`gpio_slowdown` in the web UI until it looks right, and confirm the web page
works from your phone. Everything you fix now is a thing you do not have to
talk a brother through over the phone later.

**5. Prepare it for handoff.**

```bash
sudo ./prepare-for-gift.sh
```

That forgets your wifi so Comitup falls back to AP mode in their house, resets
the setup wizard, clears caches and logs, and deletes the SSH host keys and
machine-id. That last part matters more than it sounds: cloned cards are
byte-identical, and two boards sharing a machine-id will fight over DHCP leases
and mDNS names on the same network. It deliberately **keeps** your panel
hardware settings, since the recipient has no way to work those out.

Then shut down, pull the card, and do not boot it on your wifi again.

**6. Clone for the other units.** Image the prepared card before booting it:

```bash
diskutil list                       # find the card, e.g. /dev/disk4
diskutil unmountDisk /dev/disk4
sudo dd if=/dev/rdisk4 of=~/scoreboard.img bs=4m status=progress
```

Write `scoreboard.img` to each new card with Raspberry Pi Imager. Note that
`dd` copies the entire card including free space, so a 32GB card produces a
32GB image; [PiShrink](https://github.com/Drewsif/PiShrink) shrinks it if that
is annoying. Do not use Imager's OS Customisation on the clones, or you will
write your own wifi back onto them.

**What they see.** Plug it in, wait about a minute, join the wifi network named
`scoreboard-<nnn>` from a phone. The captive portal opens by itself; they pick
their home network and enter the password. The board reboots onto their wifi,
and then the scoreboard's own wizard asks for a name, ZIP code, and teams.

## Cloning a working unit (units 2 and up)

Once one board works, do not build the next one from scratch. Writing a known-good
image takes about ten minutes and skips every step that can fail: no apt, no
compiling the LED driver, no cloud-init, no third-party repositories.

Image the working card while it is shut down, and **before** the handoff reset so
the image still carries your wifi:

```bash
diskutil list                        # find the card by SIZE, not by number
diskutil unmountDisk /dev/disk4
sudo dd if=/dev/rdisk4 of=~/scoreboard-golden.img bs=4m status=progress
```

Write it to the next card with Imager ("Use custom"), with **no OS
customisation** — that would overwrite the account and wifi that already work.

A clone is byte-identical, so it arrives with the original's hostname, SSH host
keys and machine-id. Two of those on one network collide. Boot the clone with the
original **powered off**, then give it an identity:

```bash
ssh scoreboard@rgb-scoreboard.local

sudo rm -f /etc/machine-id /var/lib/dbus/machine-id
sudo systemd-machine-id-setup
sudo rm -f /etc/ssh/ssh_host_*
sudo ssh-keygen -A

cd ~/scoreboard
echo "Kitchen Scoreboard" > state/preseed-name.txt
./install.sh --yes --no-comitup
sudo reboot
```

Your Mac will then refuse to connect, correctly, because the host key changed:

```bash
ssh-keygen -R rgb-scoreboard.local
ssh scoreboard@kitchen-scoreboard.local
```

The name cannot be set from the Mac. It lives on the ext4 partition, and the
boot-partition trick only applies to a card that has not booted yet.

Then, per unit, when it is ready to leave:

```bash
./install.sh --comitup     # needs your network, so do it before the reset
./scoreboard prepare       # forget your wifi, reset the wizard
sudo shutdown -h now
```

## Building units for other people

The board is meant to be handed to someone who will never open a terminal.

**First boot.** With Comitup installed, a board that has no wifi credentials
brings up its own access point named `scoreboard-<nnn>`. They join it from a
phone, the captive portal opens on its own, they pick their home network and
enter the password. The board reboots onto their wifi. Comitup handles the
parts that are genuinely hard here: captive-portal detection across iOS and
Android, and falling back to AP mode if the saved network disappears later.

**Then setup.** Opening the board's web page for the first time redirects to a
three-step wizard rather than the settings screen: name the board, enter a ZIP
code, add favourite teams. It ends by showing the address to bookmark.

**Naming.** The name becomes the hostname, so `Mike's Board` is reachable at
`http://mikes-board.local:8080` from any device on that network. No IP address
to find, and nothing breaks when DHCP hands out a different one. Two boards in
one house get different names and do not collide.

The rename has an awkward constraint worth knowing about. hzeller's matrix
library setuids to `daemon` after claiming GPIO, so by the time the web server
handles a rename it cannot call `hostnamectl`. Rather than run the web server
as root or add a sudoers rule, a rename writes `state/hostname.request`, and a
root-owned systemd path unit picks it up and applies it. `main.py` also applies
any pending rename at startup while it is still root, so the path unit is an
optimization rather than a requirement.

That same privilege drop is why `install.sh` sets group ownership to `daemon`
on the project directory, `config.json`, `logo_cache/`, and `state/`. Without
it, saving from the web UI fails with a permission error and logo caching
silently stops working.

**Location by ZIP.** The config takes a ZIP code, not coordinates. Nobody knows
their own latitude, and a wrong sign on the longitude puts the weather in
Kazakhstan with no visible clue why. `data/geocode.py` resolves it once via
zippopotam.us (falling back to open-meteo), caches the result to disk, and never
looks it up again. The wizard validates as they type and shows the city name
back to them, so a typo is caught immediately rather than showing someone
else's weather for a season.

## Fantasy

ESPN fantasy scores on the panel: your live matchup, and league standings.

**Setup asks for a link, not an ID.** Paste your league's ESPN address and the
board works out the league ID and whether the league is public or private. It
never asks which kind you have, because most people do not know.

- **Public league**: nothing else needed. You pick your team from a dropdown of
  real team names, since a public league gives no way to know who you are.
- **Private league**: ESPN offers no OAuth for this endpoint, so it needs two
  session values copied once from a desktop browser. The setup page gives
  click-by-click steps per browser, validates immediately, and says the league
  name back so you know it worked. Your team is inferred from the sign-in.

**Credentials never touch config.json.** They live in `state/secrets.json`,
which no API returns. This matters because `/api/config` is served whole and
unauthenticated to the local network: an ESPN session cookie there would be
readable by anyone on the wifi. The API reports only whether a credential
exists and whether it last worked. See `data/secrets.py`.

That is not encryption, and it does not pretend to be. Anyone holding the SD
card can read the file. What it buys is the difference between "needs physical
access" and "anyone on the guest wifi".

**When a sign-in expires**, which it does roughly annually, the board shows a
"sign in again" screen naming its own address rather than silently dropping the
fantasy screens, which would go unnoticed for a month.

Caveat worth knowing: this endpoint is undocumented and unsupported. ESPN moved
its host once already and broke every project using it. The code treats a
failure as normal and keeps the last good standings on screen.

## Board detection and tuning

The board identifies itself at startup and picks its own settings. A Pi Zero W
runs at 8 frames a second with reduced colour depth and a lean refresh cadence;
a Pi 3 A+ or Zero 2 W runs at 20 with full colour depth; a Pi 4 runs at 30.
Nothing is hardcoded to the weakest board this ever ran on.

| | Zero W | Zero 2 W / Pi 3 | Pi 4 / Pi 5 |
|---|---|---|---|
| Frame rate | 8 | 20 | 30 |
| Colour depth | 8 | 11 | 11 |
| GPIO slowdown | 0 | 1 | 2 |
| Refresh while live | 35s | 20s | 15s |
| Logos prefetched | 6 | 16 | 24 |

Precedence is always **explicit config > board profile > fallback**. Anything
left null in `config.json` follows the detected board; set a value and it wins.
Clear it again and the profile takes back over. A command-line flag still beats
both, for one-off testing.

The web UI shows what the board reported and what it resolved to, so Auto is a
visible decision rather than a black box. You can also pin a profile there, or
override the frame rate directly.

Why these particular knobs: the matrix library's refresh thread is the real CPU
consumer, and colour depth divides its work directly, which is why it moves
with the board. GPIO slowdown goes the other way; a fast Pi outruns the panel
and needs slowing, a Zero does not. And an HTTPS round trip costs real CPU on a
single ARMv6 core, on the same core driving the panel, so the refresh cadence
matters more there than the numbers suggest.

`rotation.max_games` is deliberately **not** in this system. How many games you
want in the loop is a preference, not a board limit.

## Panels: pitch, chaining, and sizes

**Pitch is not a setting, and cannot be.** P3, P4, P5 and P6 describe the
physical spacing between LEDs in millimetres. A 64x32 P3 panel and a 64x32 P5
panel are byte-identical to the driver; only their physical size and power draw
differ. Any project offering a "pitch" setting is either mislabelling something
else or doing nothing with it.

What genuinely varies between panels, and is configurable in the web UI under
Panel hardware:

| Setting | When to touch it |
|---|---|
| Panel width / height | Anything other than 64x32 |
| Daisy chained | Panels wired output to input, left to right |
| Parallel chains | Second or third chain off the HAT (Pi 2 and up) |
| Color depth (pwm_bits) | Lower it to 7 or 8 if a slow Pi flickers |
| GPIO slowdown | 0-1 on a Pi Zero, 2-4 on a Pi 4 |
| Multiplexing | Some outdoor P5 and P10 panels need 1-8 rather than 0 |
| Row address type | 1 for AB-addressed panels |
| Color order | If red and blue come out swapped |
| Pixel mapper | Unusual physical arrangements, e.g. `U-mapper` |

These are read at startup and need a restart, because the matrix library takes
them once at init and cannot be reconfigured live. Any command-line flag still
overrides the config for a one-off test.

**Chaining gives you more screens, not bigger ones.** Every layout here is
built for a 64x32 cell, because at this density a game needs two 20px logos and
a 23px centre column, and those do not scale by stretching. So extra panel area
becomes extra cells:

```
+---------------+---------------+
|  OSU 21 MICH  |  UGA 10 BAMA  |    chain_length 2  ->  two games at once
+---------------+---------------+
```

- 64x32, chain 1: one screen
- 128x32, chain 2: two side by side
- 192x32, chain 3: three
- 64x64: two stacked
- 128x64 (chain 2, parallel 2): four in a grid

The rotation advances all cells together, so a four-cell board cycles the whole
slate four games at a time. Turn off "One game per panel" to get a single
stretched cell instead, though no layout is currently written for one.

## Configuration

Everything is editable from the web UI. `config.json` is the same data and only
needs to contain what differs from the defaults in `data/config.py`, so an old
config keeps working after an update. An upstream `config.json` is migrated
automatically on first read.

Team codes are ESPN's abbreviations: `OSU`, `MICH`, `UGA`, `CIN`, `CLE`. If you
are unsure of one, the web UI's autocomplete is populated from teams actually
on the current slate.

The settings worth understanding:

- **stay_on_live_favorite** — when one of your teams is playing, the board stops
  rotating and stays on that game. This is the setting that makes it feel like
  a scoreboard for *your* team rather than a channel guide.
- **leave_during_halftime** — break away during halftime, when nothing is
  happening, then come back for the second half.
- **ranked_only** (college) — a game qualifies if *either* team is ranked, so an
  unranked team hanging with a top-5 team still makes the board. Your favorites
  are always included regardless of rank.
- **max_games** — caps how many games are in the loop. On a 40-game Saturday an
  uncapped rotation means minutes between looks at your team.
- **info_every** — how many games between non-game screens.

## How it fits together

```
main.py            wiring and CLI
data/
  leagues.py       league registry: ESPN paths, period naming, quirks
  espn.py          one generic parser for every sport's scoreboard document
  models.py        Game / Team / Leader, the only thing screens ever see
  store.py         background refresh thread, adaptive polling, tagging
  logos.py         fetch, crop, punch up, and cache logos at panel size
  config.py        defaults, deep merge, upstream migration, atomic save
  weather.py       open-meteo, no API key
renderer/
  layout.py        palette, fonts, and a drawable Frame
  playlist.py      what shows, in what order, for how long
  display.py       matrix backends and the frame loop
  screens/         one class per screen
web/               Flask config UI
tools/             fixtures, screenshots, capture, tests
```

Two decisions shape everything else:

**The render loop performs no I/O.** A daemon thread refreshes data on its own
cadence (about 20s while anything is live, 5 minutes otherwise) and the
renderer draws whatever snapshot is current. A slow ESPN response cannot stutter
an animation, and a failed one leaves the last good scores on screen with a
single red pixel in the corner rather than a frozen or crashed panel.

**Screens never see ESPN JSON.** The parser normalizes everything into `Game`
and `Team` first, so layout code contains no `['competitions'][0]` chains and a
second data source could be added without touching a single screen.

## Panel layout notes

A 64x32 panel with 20px logos in both top corners leaves a 23px center column.
That constraint, not taste, drives most of the layout decisions. Two that are
easy to re-break:

- The bundled `logos/` folder is NFL art keyed by NFL abbreviations, and many
  collide with college ones (MIA is both the Dolphins and the Hurricanes). Only
  the NFL reads that folder; everything else uses ESPN's marks. See
  `data/logos.py`.
- None of the bundled pixel fonts render `&` legibly at 5px; it comes out as an
  `S` or a `6`, so "3RD&7" reads as "3RDS7". Down and distance are drawn as two
  differently colored halves instead, which carries the same meaning with no
  glyph at all. See `split_down` in `renderer/screens/game.py`.

## Adding a league

ESPN's scoreboard document is identical across sports, so a league is one entry
in `data/leagues.py`:

```python
"ncaah": League(
    key="ncaah", label="WCBB", path="basketball/womens-college-basketball",
    period_fn=_college_basketball_period, query="groups=50&limit=400",
    ranked=True, rankings_path="basketball/womens-college-basketball",
),
```

Then enable it in the config. No renderer changes needed.

## Data

Everything comes from ESPN's public site API (the same one upstream used) and
open-meteo for weather. Neither needs an account or a key. Both are
undocumented or best-effort, so the store treats every request as failable.

## Credit

Built on [mikemountain/nfl-led-scoreboard](https://github.com/mikemountain/nfl-led-scoreboard),
which is itself descended from `riffnshred/nhl-led-scoreboard`. The NFL helmet
art and the touchdown and field goal animations in `assets/` are from that
project.
