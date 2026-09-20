"""ESPN Fantasy: your matchup and your league, on the panel.

ESPN has no official fantasy API. This uses the same undocumented v3 endpoint
every fantasy tool uses. It works today; ESPN moved the host once already and
broke everything, so this treats a failure as normal rather than exceptional.

Public and private leagues use the identical endpoint. The only difference is
that a private one needs two session cookies. Rather than ask which kind
someone has, which most people do not know, the code tries without cookies and
only asks for them if that fails. See `probe`.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from . import secrets

BASE = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/{game}"
        "/seasons/{year}/segments/0/leagues/{league_id}")

# ESPN's own codes for each fantasy game.
GAMES = {"football": "ffl", "basketball": "fba",
         "baseball": "flb", "hockey": "fhl"}

NAMESPACE = "espn_fantasy"
TIMEOUT = 10
USER_AGENT = "led-scoreboard/2.7"


class FantasyError(Exception):
    pass


class AuthRequired(FantasyError):
    """The league is private and the stored cookies are missing or stale."""


@dataclass
class FantasyTeam:
    id: int = 0
    name: str = ""
    abbrev: str = ""
    points: float = 0.0
    projected: float = 0.0
    wins: int = 0
    losses: int = 0
    ties: int = 0
    is_mine: bool = False

    @property
    def record(self):
        if self.ties:
            return "{}-{}-{}".format(self.wins, self.losses, self.ties)
        return "{}-{}".format(self.wins, self.losses)

    @property
    def short(self):
        return (self.abbrev or self.name or "TEAM")[:6].upper()


@dataclass
class Matchup:
    week: int = 0
    home: FantasyTeam = field(default_factory=FantasyTeam)
    away: FantasyTeam = field(default_factory=FantasyTeam)
    league_name: str = ""

    @property
    def mine(self):
        return self.home if self.home.is_mine else self.away

    @property
    def theirs(self):
        return self.away if self.home.is_mine else self.home

    @property
    def has_mine(self):
        return self.home.is_mine or self.away.is_mine

    @property
    def margin(self):
        return abs(self.home.points - self.away.points)

    @property
    def winning(self):
        """True when my team leads. None when there is no 'my team'."""
        if not self.has_mine:
            return None
        return self.mine.points >= self.theirs.points


@dataclass
class LeagueState:
    name: str = ""
    week: int = 0
    season: int = 0
    teams: list = field(default_factory=list)
    matchups: list = field(default_factory=list)
    my_matchup: object = None
    private: bool = False


# ---------------------------------------------------------------- input

LEAGUE_ID_PATTERNS = [
    r"leagueId=(\d+)",          # ...?leagueId=123456
    r"/league/(\d+)",           # .../league/123456
    r"^(\d{3,12})$",            # someone pasted just the number
]


def parse_league_id(text):
    """Pull a league ID out of whatever someone pasted.

    Asking for a 'league ID' means asking someone to find a number inside a
    URL. Asking them to paste the URL of their league is something anyone can
    do, so both are accepted.
    """
    text = str(text or "").strip()
    for pattern in LEAGUE_ID_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def clean_swid(value):
    """SWID is stored with braces. Accept it with or without."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.strip("{}")
    return "{" + text + "}"


def current_season(now=None):
    """Fantasy football's season year is the year the season starts.

    January and February still belong to the previous season's playoffs, so
    the calendar year is wrong for two months of every year.
    """
    now = now or datetime.now(timezone.utc)
    return now.year - 1 if now.month <= 2 else now.year


# ---------------------------------------------------------------- fetch

def _cookies():
    stored = secrets.get(NAMESPACE) or {}
    espn_s2 = stored.get("espn_s2")
    swid = stored.get("swid")
    if espn_s2 and swid:
        return {"espn_s2": espn_s2, "SWID": clean_swid(swid)}
    return None


def fetch(league_id, year=None, game="football", views=None, week=None,
          use_cookies=True):
    """Raw league payload. Raises AuthRequired when the league is private."""
    league_id = parse_league_id(league_id)
    if not league_id:
        raise FantasyError("no league ID")

    url = BASE.format(game=GAMES.get(game, "ffl"),
                      year=year or current_season(),
                      league_id=league_id)
    params = []
    for view in (views or ["mMatchupScore", "mTeam", "mSettings"]):
        params.append(("view", view))
    if week:
        params.append(("scoringPeriodId", str(week)))

    cookies = _cookies() if use_cookies else None
    try:
        resp = requests.get(url, params=params, cookies=cookies,
                            timeout=TIMEOUT,
                            headers={"User-Agent": USER_AGENT})
    except Exception as exc:
        raise FantasyError("could not reach ESPN: {}".format(exc))

    # 401 is a private league with no or stale cookies. ESPN also returns 403
    # in some cases, and an HTML error page rather than JSON in others.
    if resp.status_code in (401, 403):
        raise AuthRequired("league is private and needs an ESPN login")
    if resp.status_code == 404:
        raise FantasyError("league {} not found for {}".format(
            league_id, year or current_season()))
    if resp.status_code >= 400:
        raise FantasyError("ESPN returned {}".format(resp.status_code))

    try:
        data = resp.json()
    except Exception:
        raise FantasyError("ESPN returned something that is not JSON")

    # A list wrapper appears on some season/league combinations.
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise FantasyError("unexpected response shape")
    return data


def probe(league_id, year=None, game="football"):
    """Work out what this league needs, without asking the user.

    Returns one of:
      {"ok": True,  "private": False, "name": ...}   public, nothing needed
      {"ok": True,  "private": True,  "name": ...}   private, stored cookies work
      {"ok": False, "needs_auth": True}              private, needs cookies
      {"ok": False, "error": "..."}                  wrong ID, season, or ESPN down
    """
    league_id = parse_league_id(league_id)
    if not league_id:
        return {"ok": False, "error": "That does not look like a league link"}

    # Public leagues answer without cookies. Try that first so a public league
    # never gets asked for a login it does not need.
    try:
        data = fetch(league_id, year, game, views=["mSettings"],
                     use_cookies=False)
        return {"ok": True, "private": False, "league_id": league_id,
                "name": (data.get("settings") or {}).get("name", "")}
    except AuthRequired:
        pass
    except FantasyError as exc:
        return {"ok": False, "error": str(exc)}

    if not secrets.has(NAMESPACE, "espn_s2", "swid"):
        return {"ok": False, "needs_auth": True, "league_id": league_id,
                "error": "This league is private, so it needs your ESPN login"}

    try:
        data = fetch(league_id, year, game, views=["mSettings"])
    except AuthRequired:
        return {"ok": False, "needs_auth": True, "league_id": league_id,
                "error": "Your saved ESPN login is no longer valid"}
    except FantasyError as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": True, "private": True, "league_id": league_id,
            "name": (data.get("settings") or {}).get("name", "")}


# ---------------------------------------------------------------- parse

def _team_name(raw):
    """ESPN has used three different shapes for a team's name over the years."""
    name = (raw.get("name") or "").strip()
    if not name:
        name = " ".join(filter(None, [(raw.get("location") or "").strip(),
                                      (raw.get("nickname") or "").strip()]))
    return name.strip() or "Team {}".format(raw.get("id", "?"))


def _parse_teams(payload, my_team_ids):
    teams = {}
    for raw in payload.get("teams") or []:
        record = ((raw.get("record") or {}).get("overall") or {})
        team = FantasyTeam(
            id=raw.get("id", 0),
            name=_team_name(raw),
            abbrev=(raw.get("abbrev") or "").upper(),
            wins=int(record.get("wins") or 0),
            losses=int(record.get("losses") or 0),
            ties=int(record.get("ties") or 0),
            is_mine=raw.get("id") in my_team_ids,
        )
        teams[team.id] = team
    return teams


def _side_points(side):
    """Live points for one side of a matchup.

    totalPoints is the settled score and lags during games; the roster's
    appliedStatTotal updates live. Prefer whichever is larger, which is the
    live one while a game is running and the settled one afterwards.
    """
    total = float(side.get("totalPoints") or 0.0)
    roster = side.get("rosterForCurrentScoringPeriod") or {}
    live = float(roster.get("appliedStatTotal") or 0.0)
    return max(total, live)


def _my_team_ids(payload):
    """Which team is mine, from the SWID in the stored cookies."""
    swid = clean_swid((secrets.get(NAMESPACE) or {}).get("swid"))
    ids = set()
    for raw in payload.get("teams") or []:
        owners = raw.get("owners") or []
        if swid and swid in owners:
            ids.add(raw.get("id"))
        if raw.get("isMyTeam") or raw.get("currentProjectedRank") == -1:
            pass  # not reliable; left here as a reminder not to trust it
    return ids


def league_state(league_id, year=None, game="football", week=None,
                 my_team_id=None):
    """Everything the screens need, in one call."""
    payload = fetch(league_id, year, game,
                    views=["mMatchupScore", "mTeam", "mSettings"], week=week)

    settings = payload.get("settings") or {}
    status = payload.get("status") or {}
    current_week = int(week or status.get("currentMatchupPeriod")
                       or payload.get("scoringPeriodId") or 1)

    mine = _my_team_ids(payload)
    if my_team_id:
        mine.add(int(my_team_id))
    teams = _parse_teams(payload, mine)

    state = LeagueState(
        name=settings.get("name", "") or "Fantasy",
        week=current_week,
        season=int(year or current_season()),
        teams=sorted(teams.values(), key=lambda t: (-t.wins, t.losses, t.name)),
        private=bool(_cookies()),
    )

    for entry in payload.get("schedule") or []:
        if int(entry.get("matchupPeriodId") or 0) != current_week:
            continue
        home_raw = entry.get("home") or {}
        away_raw = entry.get("away") or {}
        home = teams.get(home_raw.get("teamId"))
        away = teams.get(away_raw.get("teamId"))
        if not home or not away:
            continue

        # Copy so per-matchup points do not leak into the standings list.
        home = FantasyTeam(**{**vars(home), "points": _side_points(home_raw)})
        away = FantasyTeam(**{**vars(away), "points": _side_points(away_raw)})

        matchup = Matchup(week=current_week, home=home, away=away,
                          league_name=state.name)
        state.matchups.append(matchup)
        if matchup.has_mine:
            state.my_matchup = matchup

    return state
