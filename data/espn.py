"""ESPN site-API client and parser.

One generic parser covers every league because ESPN's scoreboard document has
the same shape for all sports. Anything sport-specific is delegated to the
League record in leagues.py.

This module is deliberately free of any rendering or threading concerns so it
can be exercised straight from a REPL or against saved fixtures:

    python3 -m data.espn nfl        # print today's slate
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from . import leagues
from .models import Game, Leader, RankingEntry, StandingEntry, Team

BASE = "https://site.api.espn.com/apis/site/v2/sports"
CORE = "https://sports.core.api.espn.com/v2/sports"
TIMEOUT = 8
USER_AGENT = "led-scoreboard/2.0 (+https://github.com/mikemountain/nfl-led-scoreboard)"

# Point FIXTURE_DIR at a folder of saved JSON to develop with no network.
FIXTURE_DIR = os.environ.get("SCOREBOARD_FIXTURES")


class EspnError(Exception):
    pass


def _fixture_name(url: str) -> str:
    keep = url.replace(BASE + "/", "").replace("/", "_")
    for ch in "?&=":
        keep = keep.replace(ch, "_")
    return keep[:120] + ".json"


def _get(url: str, retries: int = 3):
    """GET with retry/backoff, or read a fixture when running offline."""
    if FIXTURE_DIR:
        path = os.path.join(FIXTURE_DIR, _fixture_name(url))
        if os.path.exists(path):
            with open(path) as fh:
                return json.load(fh)
        raise EspnError("no fixture for {}".format(url))

    last = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=TIMEOUT,
                                headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:          # network, JSON, HTTP all handled alike
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise EspnError("GET {} failed: {}".format(url, last))


def save_fixture(url: str, directory: str):
    """Helper for capturing live responses to develop against offline."""
    os.makedirs(directory, exist_ok=True)
    data = _get(url)
    with open(os.path.join(directory, _fixture_name(url)), "w") as fh:
        json.dump(data, fh)
    return data


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def _parse_dt(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%MZ",
                "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_team(node, league) -> Team:
    raw = node.get("team", {}) or {}
    team = Team(
        id=str(raw.get("id", "")),
        abbr=(raw.get("abbreviation") or raw.get("shortDisplayName") or "")[:5].upper(),
        name=raw.get("name", ""),
        location=raw.get("location", ""),
        display=raw.get("displayName", ""),
        score=_int(node.get("score")),
        color=(raw.get("color") or "ffffff").lstrip("#"),
        alt_color=(raw.get("alternateColor") or "000000").lstrip("#"),
        logo_url=raw.get("logo") or "",
        winner=bool(node.get("winner")),
        home=node.get("homeAway") == "home",
    )

    if not team.logo_url:
        logos = raw.get("logos") or []
        if logos:
            team.logo_url = logos[0].get("href", "")

    if league.ranked:
        rank = (node.get("curatedRank") or {}).get("current")
        if rank and rank <= 25:
            team.rank = rank

    for rec in node.get("records") or []:
        if rec.get("type") in ("total", "overall") or rec.get("name") == "overall":
            team.record = rec.get("summary", "")
        elif rec.get("type") == "vsconf":
            team.conf_record = rec.get("summary", "")

    # Baseball/basketball per-period scores, used by the linescore screen
    team.linescores = [_int(ls.get("value")) for ls in node.get("linescores") or []]
    return team


def _parse_leaders(competition) -> list:
    """Top performer per statistical category, trimmed for a 64px display."""
    out = []
    for group in competition.get("leaders") or []:
        entries = group.get("leaders") or []
        if not entries:
            continue
        best = entries[0]
        athlete = (best.get("athlete") or {})
        name = athlete.get("shortName") or athlete.get("displayName") or ""
        # "C. Stroud" -> "STROUD"
        surname = name.split(" ")[-1].upper() if name else ""
        abbrev = (group.get("abbreviation") or group.get("shortDisplayName")
                  or group.get("name") or "").upper()
        out.append(Leader(
            category=abbrev[:6],
            athlete=surname[:10],
            value=(best.get("displayValue") or "")[:22],
            team_abbr=str((athlete.get("team") or {}).get("abbreviation", "")),
        ))
    return out


def parse_event(event, league) -> Game:
    comps = event.get("competitions") or []
    if not comps:
        raise EspnError("event {} has no competition".format(event.get("id")))
    comp = comps[0]
    status = comp.get("status") or event.get("status") or {}
    stype = status.get("type") or {}

    game = Game(
        id=str(event.get("id", "")),
        league=league.key,
        league_label=league.label,
        start=_parse_dt(event.get("date")),
        state=stype.get("state", "pre"),
        detail=stype.get("shortDetail") or stype.get("detail") or "",
        clock=status.get("displayClock") or "",
        period=_int(status.get("period")),
        neutral_site=bool(comp.get("neutralSite")),
    )
    game.period_label = league.period_fn(game.period, game.state, game.detail)

    for node in comp.get("competitors") or []:
        team = _parse_team(node, league)
        if team.home:
            game.home = team
        else:
            game.away = team

    if league.downs:
        sit = comp.get("situation") or {}
        game.down_distance = sit.get("shortDownDistanceText") or ""
        game.yard_line = sit.get("possessionText") or ""
        game.possession_id = str(sit.get("possession") or "")
        game.red_zone = bool(sit.get("isRedZone"))

    broadcasts = comp.get("broadcasts") or []
    if broadcasts:
        names = broadcasts[0].get("names") or []
        game.broadcast = names[0] if names else ""

    venue = comp.get("venue") or {}
    game.venue = (venue.get("fullName") or "")[:40]

    odds = comp.get("odds") or []
    if odds:
        game.odds = (odds[0].get("details") or "")[:14]
        ou = odds[0].get("overUnder")
        game.over_under = "O/U {}".format(ou) if ou else ""

    notes = comp.get("notes") or []
    if notes:
        game.headline = (notes[0].get("headline") or "")[:60]

    game.leaders = _parse_leaders(comp)
    game.is_ranked_matchup = bool(game.home.rank and game.away.rank)
    return game


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------

def scoreboard(league_key: str, dates=None) -> list:
    """Games for a league. `dates` is 'YYYYMMDD' or 'YYYYMMDD-YYYYMMDD'."""
    league = leagues.get(league_key)
    if not league:
        raise EspnError("unknown league {}".format(league_key))

    url = "{}/{}/scoreboard".format(BASE, league.path)
    params = []
    if league.query:
        params.append(league.query)
    if dates:
        params.append("dates={}".format(dates))
    if params:
        url += "?" + "&".join(params)

    payload = _get(url)
    games = []
    for event in payload.get("events") or []:
        try:
            games.append(parse_event(event, league))
        except Exception:
            continue
    return games


def scoreboard_window(league_key: str) -> list:
    """Today plus the league's lookahead window, de-duplicated by game id.

    ESPN's default scoreboard is "today only", which means on a Thursday a
    college board would show three MACtion games and nothing else. Widening
    the window is what makes the Saturday slate visible mid-week.
    """
    league = leagues.get(league_key)
    games = scoreboard(league_key)
    if league and league.lookahead_days:
        today = datetime.now(timezone.utc)
        end = today + timedelta(days=league.lookahead_days)
        span = "{}-{}".format(today.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        try:
            games += scoreboard(league_key, dates=span)
        except EspnError:
            pass
    seen, unique = set(), []
    for game in games:
        if game.id in seen:
            continue
        seen.add(game.id)
        unique.append(game)
    return unique


def rankings(league_key: str, poll: str = "ap") -> list:
    """AP (or CFP/coaches) Top 25."""
    league = leagues.get(league_key)
    if not league or not league.rankings_path:
        return []
    payload = _get("{}/{}/rankings".format(BASE, league.rankings_path))

    polls = payload.get("rankings") or []
    if not polls:
        return []

    wanted = poll.lower()
    chosen = polls[0]
    for entry in polls:
        name = (entry.get("shortName") or entry.get("name") or "").lower()
        if wanted in name or (wanted == "cfp" and "playoff" in name):
            chosen = entry
            break

    out = []
    for row in chosen.get("ranks") or []:
        team = row.get("team") or {}
        out.append(RankingEntry(
            rank=_int(row.get("current")),
            abbr=(team.get("abbreviation") or team.get("shortDisplayName") or "")[:5].upper(),
            name=team.get("nickname") or team.get("name") or team.get("location", ""),
            record=(row.get("recordSummary") or ""),
            points=str(row.get("points") or ""),
            trend=str(row.get("trend") or "-"),
            logo_url=(team.get("logos") or [{}])[0].get("href", ""),
        ))
    return out


def standings(league_key: str) -> list:
    """Flat list of standing entries across all divisions/conferences."""
    league = leagues.get(league_key)
    if not league or not league.standings_path:
        return []

    payload = _get("{}/{}/standings".format(BASE, league.standings_path))
    out = []

    def walk(node, group_name=""):
        name = node.get("name") or node.get("displayName") or group_name
        standings_node = node.get("standings") or {}
        for entry in standings_node.get("entries") or []:
            team = entry.get("team") or {}
            stats = {s.get("name"): s for s in entry.get("stats") or []}

            def stat(key, default=""):
                s = stats.get(key) or {}
                return s.get("displayValue", default)

            out.append(StandingEntry(
                abbr=(team.get("abbreviation") or "")[:5].upper(),
                name=team.get("displayName") or team.get("name", ""),
                wins=_int(stat("wins", 0)),
                losses=_int(stat("losses", 0)),
                ties=_int(stat("ties", 0)),
                pct=stat("winPercent"),
                games_back=stat("gamesBehind"),
                streak=stat("streak"),
                division=name,
                logo_url=(team.get("logos") or [{}])[0].get("href", ""),
            ))
        for child in node.get("children") or []:
            walk(child, name)

    walk(payload)
    return out


if __name__ == "__main__":
    import sys
    key = sys.argv[1] if len(sys.argv) > 1 else "nfl"
    for g in scoreboard_window(key):
        print(g)
