"""Configuration loading, validation and hot reload.

config.json only needs to contain what differs from DEFAULTS, so a config
written by an older version keeps working after an update instead of throwing
a KeyError the way the upstream config did.
"""

import copy
import json
import os
import threading

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

DEFAULTS = {
    # Which leagues to pull, and who you care about in each. Abbreviations are
    # ESPN's: OSU, MICH, CIN, ND, UGA ... check a scoreboard URL if unsure.
    "leagues": {
        "nfl":   {"enabled": True,  "favorites": ["CIN", "CLE"]},
        "ncaaf": {"enabled": True,  "favorites": ["OSU"], "poll": "ap",
                  "ranked_only": True},
        "nba":   {"enabled": False, "favorites": ["CLE"]},
        "ncaam": {"enabled": False, "favorites": ["OSU"], "ranked_only": True},
        "mlb":   {"enabled": False, "favorites": ["CLE"]},
        "nhl":   {"enabled": False, "favorites": ["CBJ"]},
        "wnba":  {"enabled": False, "favorites": []},
        "epl":   {"enabled": False, "favorites": []},
        "ucl":   {"enabled": False, "favorites": []},
        "mls":   {"enabled": False, "favorites": ["CLB"]},
    },

    # Self-update. A board given away sits on a network you cannot reach, so
    # the only way to fix anything on it is for the board to come and ask.
    "updates": {
        # The recipient can switch this off, and should be told it exists.
        "enabled": True,
        # "stable" for a board in someone else's house, "beta" for your own.
        # Your board being the only one on beta is what makes a bad release
        # something you find out about rather than something three people do.
        "channel": "stable",
        # Signed manifest listing what each channel should be running. Empty
        # means no self-update: the board checks nothing and installs nothing.
        "manifest_url": "",
    },
    "rotation": {
        "enabled": True,
        # Only leagues playing today are in the rotation. Turn off for a board
        # that should show everything ESPN returns, whenever it was played.
        "day_relevance": True,
        # How often a favourite from a league that is not playing today comes
        # round: 3 means one pass in three. 1 means every pass.
        "offday_favorite_every": 3,
        # Show only games involving a favorite team, ignoring everything else.
        "favorites_only": False,
        # When a favorite is playing live, stop rotating and camp on that game.
        "stay_on_live_favorite": True,
        # ...but still break away during halftime, when nothing is happening.
        "leave_during_halftime": True,
        # Seconds per screen, by screen kind.
        "rates": {
            "live": 12.0,
            "pregame": 8.0,
            "final": 8.0,
            "info": 10.0,
        },
        # Cap how many non-favorite games make the loop, so a 40-game Saturday
        # does not mean five minutes between passes at your team. Favourites are
        # never dropped by this: the cap applies to everything else, and the
        # remaining room is shared out between leagues by taking turns so one
        # busy slate cannot crowd the other off the board entirely.
        "max_games": 14,
        # Walk one league at a time -- all the college games, then all the NFL
        # ones -- instead of interleaving them by kickoff. Two sports whose
        # scores look alike are hard to read as a single mixed stream.
        "group_by_league": True,
        # Which league goes first. Empty means "the order they are enabled in".
        "league_order": [],
    },

    # Non-game screens mixed into the rotation.
    "screens": {
        "rankings": True,        # AP Top 25, three teams at a time
        "standings": True,       # division standings
        "leaders": True,         # per-game statistical leaders
        "team_summary": True,    # favorite team record / next game card
        "clock": True,
        "weather": True,
        "ticker": True,          # scrolling all-scores line
        # How many game screens between each info screen.
        "info_every": 4,
    },

    # Panel hardware. Changing anything here needs a restart, because the
    # matrix library takes these once at init and cannot be reconfigured live.
    #
    # Pitch (P3, P4, P5, P6) is NOT here on purpose: it is the physical spacing
    # between LEDs and has no effect on the driver. A 64x32 P3 and a 64x32 P5
    # are byte-identical to the software; only the panel's physical size and
    # power draw differ. What actually varies between panels, and does matter,
    # is scan rate, multiplexing and row addressing, which are the settings
    # below.
    "matrix": {
        "rows": 32,             # pixel rows on ONE panel
        "cols": 64,             # pixel columns on ONE panel
        "chain_length": 1,      # panels daisy-chained left to right
        "parallel": 1,          # parallel chains (Pi 2 and up, 1..3)
        "gpio_mapping": "adafruit-hat",
        # None so display.brightness stays the single live control; this exists
        # only so a CLI flag has somewhere to land.
        "brightness": None,
        # null on these three means "pick from the detected board" (see
        # data/hardware.py). Set a number to override for this board.
        "pwm_bits": None,       # 7-8 on a slow Pi, 11 on anything quad-core
        "pwm_lsb_nanoseconds": 130,
        "gpio_slowdown": None,  # blank follows the board profile
        "scan_mode": 1,         # 0 progressive, 1 interlaced
        "multiplexing": 0,      # 0 direct; some outdoor P5/P10 panels need 1-8
        "row_address_type": 0,  # 0 default; 1 for AB-addressed panels
        "rgb_sequence": "RGB",  # some panels wire the colours differently
        "pixel_mapper": "",     # e.g. "U-mapper" or "Rotate:90"
        "no_hardware_pulse": False,
        "limit_refresh_hz": 0,  # 0 = unlimited; capping can steady a slow Pi
        # With more than one 64x32 cell of space, show a different screen in
        # each rather than stretching one screen across the whole thing.
        "tile_screens": True,
    },

    "display": {
        "brightness": 60,
        "use_helmet_logos": True,   # NFL only; college always uses ESPN marks
        # auto: use a logo unless it is too dark or sparse to read at 20px
        # logo: always try the logo
        # text: always draw the abbreviation in team colour
        "logo_style": "auto",
        # Teams whose mark never survives the shrink, drawn as text instead.
        # A block letter with a hollow centre is the usual culprit: the thin
        # keyline that makes it a letter averages away, and what is left is a
        # coloured blob with a hole in it.
        "logo_text_teams": [],
        # A drawn hairline between cells on a chained panel. Off, because the
        # physical gap between two panels already separates them and the line
        # lands a pixel inside the left panel, where it reads as a lit bar down
        # its edge rather than as a divider.
        "cell_divider": False,
        "time_format": "12h",
        # IANA name, e.g. "America/New_York". Empty means "use the Pi's system
        # timezone", which is UTC on a fresh SD card and silently shows every
        # kickoff several hours off. Setting it here makes the board correct
        # regardless of how the OS is configured.
        "timezone": "",
        "scroll_speed": 0.04,       # seconds per pixel step, lower is faster
        "sleep": {                  # blank the panel overnight
            "enabled": False,
            "start": "23:30",
            "end": "07:00",
        },
    },

    # What this board is called. Drives the hostname, so the board is reachable
    # at <hostname>.local instead of an IP address that DHCP may change.
    "identity": {
        "name": "Scoreboard",
        "hostname": "scoreboard",
    },

    # Set by ZIP code; coordinates are derived and cached by data/geocode.py.
    # Nobody knows their own latitude, and a wrong sign on the longitude puts
    # the weather in Kazakhstan with no visible clue why.
    "location": {
        "zip": "43215",
        "latitude": None,
        "longitude": None,
        "city": "",
        "state": "",
        "resolved_zip": "",
    },

    # open-meteo, no API key required.
    "weather": {
        "enabled": True,
        "units": "imperial",
    },

    # ESPN fantasy. Credentials for a private league are NOT here: they live
    # in state/secrets.json, which the web API never returns. See data/secrets.py.
    "fantasy": {
        "enabled": False,
        "league_url": "",        # what the user pasted, kept for the UI
        "league_id": "",         # parsed out of it
        "game": "football",
        "team_id": None,         # only needed if we cannot infer it from SWID
        "show_matchup": True,
        "show_standings": True,
        "refresh_seconds": 120,
    },

    # False until first-run setup finishes, which is what makes the web UI
    # open the onboarding wizard instead of the settings page.
    "setup": {
        "complete": False,
    },

    "web": {
        "enabled": True,
        "port": 8080,
    },

    # Anything null here is chosen from the detected board rather than being
    # fixed at the speed of the weakest Pi this ever ran on. A Zero W gets 8
    # frames a second and a lean refresh cadence; a Pi 3 A+ or better gets the
    # full rate. Setting a value pins it for this board.
    "performance": {
        "profile": "auto",       # auto | zero_w | quad_small | pi4 | pi5 | generic
        "frame_rate": None,
        "logo_prefetch": None,
    },

    "refresh": {
        "live_seconds": None,    # while any tracked game is in progress
        "idle_seconds": None,    # when nothing is live
        "rankings_seconds": 3600,
        "standings_seconds": 3600,
    },

    "debug": False,
}


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Dict-backed config with dotted lookup and atomic save."""

    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = copy.deepcopy(DEFAULTS)
        self.version = 0          # bumped on every write, watched by the loop
        self.load()

    # ---------- access ----------

    def get(self, dotted, default=None):
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted, value):
        parts = dotted.split(".")
        with self._lock:
            node = self._data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            self.version += 1

    @property
    def data(self):
        return copy.deepcopy(self._data)

    # ---------- derived helpers ----------

    def enabled_leagues(self):
        return [key for key, cfg in (self.get("leagues") or {}).items()
                if cfg.get("enabled")]

    def favorites(self, league_key):
        return [f.upper() for f in
                self.get("leagues.{}.favorites".format(league_key), []) or []]

    def all_favorites(self):
        out = {}
        for key in self.enabled_leagues():
            out[key] = self.favorites(key)
        return out

    def ranked_only(self, league_key):
        return bool(self.get("leagues.{}.ranked_only".format(league_key), False))

    def rate(self, kind):
        return float(self.get("rotation.rates.{}".format(kind), 10.0))

    # ---------- persistence ----------

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as fh:
                user = json.load(fh)
        except Exception as exc:
            print("config: {} is not valid JSON ({}), using defaults".format(
                self.path, exc))
            return
        with self._lock:
            self._data = _deep_merge(DEFAULTS, self._migrate(user))
            self.version += 1

    def replace(self, new_data):
        with self._lock:
            self._data = _deep_merge(DEFAULTS, new_data)
            self.version += 1
        self.save()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self._data, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, self.path)

    @staticmethod
    def _migrate(user):
        """Accept older configs, including upstream's, unchanged."""
        # v2.0 stored coordinates directly under weather. Carry them across so
        # an existing board keeps its weather until the ZIP resolves.
        weather = user.get("weather") or {}
        if "latitude" in weather and "location" not in user:
            user = dict(user)
            user["location"] = {
                "zip": user.get("location", {}).get("zip", ""),
                "latitude": weather.get("latitude"),
                "longitude": weather.get("longitude"),
            }
            user["weather"] = {k: v for k, v in weather.items()
                               if k not in ("latitude", "longitude")}

        if "preferred" in user and "leagues" not in user:
            teams = (user.get("preferred") or {}).get("teams") or []
            rot = user.get("rotation") or {}
            rates = rot.get("rates") or {}
            return {
                "leagues": {"nfl": {"enabled": True, "favorites": teams}},
                "rotation": {
                    "enabled": rot.get("enabled", True),
                    "favorites_only": rot.get("only_preferred", False),
                    "rates": {
                        "live": rates.get("live", 12.0),
                        "pregame": rates.get("pregame", 8.0),
                        "final": rates.get("final", 8.0),
                    },
                },
                "display": {
                    "use_helmet_logos": user.get("use_helmet_logos", True)},
                "debug": bool(user.get("debug")),
            }
        return user
