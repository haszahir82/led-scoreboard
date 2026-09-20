"""Synthetic ESPN-shaped payloads.

Used two ways: to exercise the real parser without a network, and to drive the
screenshot tool so layouts can be checked in July.

`python3 -m tools.capture` replaces these with real captured responses once you
are somewhere that can reach ESPN.
"""

from datetime import datetime, timedelta, timezone

LOGO = "https://a.espncdn.com/i/teamlogos/ncaa/500/{}.png"


def _competitor(tid, abbr, location, name, score, home, color, rank=None,
                record="0-0", conf="0-0", winner=False, logo=None):
    node = {
        "id": str(tid),
        "homeAway": "home" if home else "away",
        "score": str(score),
        "winner": winner,
        "team": {
            "id": str(tid), "abbreviation": abbr, "location": location,
            "name": name, "displayName": "{} {}".format(location, name),
            "shortDisplayName": location, "color": color,
            "alternateColor": "ffffff",
            "logo": logo or LOGO.format(tid),
        },
        "records": [
            {"name": "overall", "type": "total", "summary": record},
            {"name": "vs. Conf.", "type": "vsconf", "summary": conf},
        ],
    }
    if rank is not None:
        node["curatedRank"] = {"current": rank}
    return node


def _event(eid, away, home, state, detail, clock="", period=0, situation=None,
           start=None, broadcast="CBS", odds=None, leaders=None, notes=None):
    start = start or datetime.now(timezone.utc)
    comp = {
        "id": str(eid),
        "competitors": [home, away],          # ESPN lists home first
        "status": {
            "displayClock": clock, "period": period,
            "type": {"state": state, "shortDetail": detail, "detail": detail,
                     "completed": state == "post"},
        },
        "broadcasts": [{"names": [broadcast]}] if broadcast else [],
        "venue": {"fullName": "Ohio Stadium"},
        "neutralSite": False,
    }
    if situation:
        comp["situation"] = situation
    if odds:
        comp["odds"] = [{"details": odds[0], "overUnder": odds[1]}]
    if leaders:
        comp["leaders"] = leaders
    if notes:
        comp["notes"] = [{"headline": notes}]
    return {
        "id": str(eid),
        "date": start.strftime("%Y-%m-%dT%H:%MZ"),
        "shortName": "{} @ {}".format(away["team"]["abbreviation"],
                                      home["team"]["abbreviation"]),
        "competitions": [comp],
        "status": comp["status"],
    }


def _leaders():
    def group(abbrev, name, athlete, value, team):
        return {
            "name": name, "abbreviation": abbrev,
            "leaders": [{
                "displayValue": value,
                "athlete": {"shortName": athlete, "displayName": athlete,
                            "team": {"abbreviation": team}},
            }],
        }
    return [
        group("PASS", "passingYards", "W. Howard", "312 YDS, 3 TD", "OSU"),
        group("RUSH", "rushingYards", "Q. Judkins", "108 YDS, 1 TD", "OSU"),
        group("REC", "receivingYards", "J. Smith", "141 YDS, 2 TD", "OSU"),
    ]


def ncaaf_scoreboard():
    now = datetime.now(timezone.utc)
    events = [
        # Live, red zone, ranked matchup, favourite involved
        _event(
            401001,
            _competitor(194, "OSU", "Ohio State", "Buckeyes", 21, False,
                        "bb0000", rank=2, record="4-0", conf="1-0"),
            _competitor(130, "MICH", "Michigan", "Wolverines", 17, True,
                        "00274c", rank=6, record="4-1", conf="1-1"),
            "in", "9:31 - 3rd", clock="9:31", period=3,
            situation={"shortDownDistanceText": "3rd & 7",
                       "possessionText": "MICH 12",
                       "possession": "194", "isRedZone": True},
            leaders=_leaders(), broadcast="FOX"),
        # Live, normal drive, long clock
        _event(
            401002,
            _competitor(61, "UGA", "Georgia", "Bulldogs", 10, False,
                        "ba0c2f", rank=1, record="5-0"),
            _competitor(333, "BAMA", "Alabama", "Crimson Tide", 14, True,
                        "9e1b32", rank=8, record="4-1"),
            "in", "2:04 - 2nd", clock="2:04", period=2,
            situation={"shortDownDistanceText": "1st & 10",
                       "possessionText": "UGA 45", "possession": "61",
                       "isRedZone": False},
            leaders=_leaders(), broadcast="ABC"),
        # Halftime
        _event(
            401003,
            _competitor(2390, "MIA", "Miami", "Hurricanes", 24, False,
                        "f47321", rank=11, record="5-0"),
            _competitor(52, "FSU", "Florida State", "Seminoles", 7, True,
                        "782f40", record="1-4"),
            "in", "Halftime", clock="0:00", period=2, broadcast="ESPN"),
        # Pregame, later today, with a line
        _event(
            401004,
            _competitor(87, "ND", "Notre Dame", "Fighting Irish", 0, False,
                        "0c2340", rank=13, record="4-1"),
            _competitor(2294, "IOWA", "Iowa", "Hawkeyes", 0, True,
                        "000000", record="3-2"),
            "pre", "Sat 7:30 PM ET", start=now + timedelta(hours=5),
            odds=("ND -6.5", 41.5), broadcast="NBC"),
        # Pregame, kicking off within the hour (countdown path)
        _event(
            401005,
            _competitor(2633, "TENN", "Tennessee", "Volunteers", 0, False,
                        "ff8200", rank=9, record="4-0"),
            _competitor(99, "LSU", "LSU", "Tigers", 0, True, "461d7c",
                        rank=20, record="3-2"),
            "pre", "Sat 3:30 PM ET", start=now + timedelta(minutes=38),
            odds=("TENN -3", 52.5), broadcast="CBS"),
        # Final, with OT
        _event(
            401006,
            _competitor(2483, "ORE", "Oregon", "Ducks", 34, False, "154733",
                        rank=3, record="5-0", winner=True),
            _competitor(264, "WASH", "Washington", "Huskies", 31, True,
                        "4b2e83", record="3-2"),
            "post", "Final/OT", period=5,
            start=now - timedelta(hours=4),
            notes="Ducks survive in Seattle", leaders=_leaders()),
        # Final, unranked blowout (tests the ranked_only filter)
        _event(
            401007,
            _competitor(2005, "AKR", "Akron", "Zips", 3, False, "00285e",
                        record="1-5"),
            _competitor(2649, "TOL", "Toledo", "Rockets", 45, True, "003e7e",
                        record="4-2", winner=True),
            "post", "Final", start=now - timedelta(hours=6), broadcast=""),
    ]
    return {"events": events, "week": {"number": 6},
            "season": {"year": 2026, "type": 2}}


def nfl_scoreboard():
    now = datetime.now(timezone.utc)
    events = [
        _event(
            402001,
            _competitor(4, "CIN", "Cincinnati", "Bengals", 27, False, "fb4f14",
                        record="3-2"),
            _competitor(5, "CLE", "Cleveland", "Browns", 24, True, "311d00",
                        record="2-3"),
            "in", "1:12 - 4th", clock="1:12", period=4,
            situation={"shortDownDistanceText": "2nd & Goal",
                       "possessionText": "CLE 4", "possession": "5",
                       "isRedZone": True},
            leaders=_leaders(), broadcast="CBS"),
        _event(
            402002,
            _competitor(33, "BAL", "Baltimore", "Ravens", 0, False, "24135f",
                        record="4-1"),
            _competitor(23, "PIT", "Pittsburgh", "Steelers", 0, True, "ffb612",
                        record="3-2"),
            "pre", "Sun 1:00 PM ET", start=now + timedelta(hours=20),
            odds=("BAL -3.5", 44.5), broadcast="CBS"),
        _event(
            402003,
            _competitor(12, "KC", "Kansas City", "Chiefs", 31, False, "e31837",
                        record="5-0", winner=True),
            _competitor(24, "LAC", "LA Chargers", "Chargers", 17, True,
                        "0080c6", record="2-3"),
            "post", "Final", start=now - timedelta(hours=3),
            notes="Chiefs stay unbeaten"),
    ]
    return {"events": events, "week": {"number": 5},
            "season": {"year": 2026, "type": 2}}


def rankings():
    teams = [
        (194, "OSU", "Buckeyes", "4-0"), (61, "UGA", "Bulldogs", "5-0"),
        (2483, "ORE", "Ducks", "5-0"), (99, "LSU", "Tigers", "3-2"),
        (2633, "TENN", "Volunteers", "4-0"), (130, "MICH", "Wolverines", "4-1"),
        (333, "BAMA", "Crimson Tide", "4-1"), (87, "ND", "Fighting Irish", "4-1"),
        (2390, "MIA", "Hurricanes", "5-0"), (251, "TEX", "Longhorns", "4-1"),
        (2, "AUB", "Tigers", "3-2"), (145, "OU", "Sooners", "4-1"),
    ]
    ranks = []
    for i, (tid, abbr, name, record) in enumerate(teams, start=1):
        ranks.append({
            "current": i, "previous": i + 1, "trend": "+1",
            "points": str(1500 - i * 40), "recordSummary": record,
            "team": {"id": str(tid), "abbreviation": abbr, "nickname": name,
                     "location": name, "name": name,
                     "logos": [{"href": LOGO.format(tid)}]},
        })
    return {"rankings": [{"name": "AP Top 25", "shortName": "AP Poll",
                          "ranks": ranks}]}


def nfl_standings():
    def entry(abbr, name, wins, losses, streak):
        return {
            "team": {"abbreviation": abbr, "displayName": name, "logos": []},
            "stats": [
                {"name": "wins", "displayValue": str(wins)},
                {"name": "losses", "displayValue": str(losses)},
                {"name": "ties", "displayValue": "0"},
                {"name": "winPercent", "displayValue": ".600"},
                {"name": "streak", "displayValue": streak},
            ],
        }
    return {
        "name": "National Football League",
        "children": [{
            "name": "AFC North",
            "standings": {"entries": [
                entry("PIT", "Pittsburgh Steelers", 4, 1, "W2"),
                entry("BAL", "Baltimore Ravens", 4, 1, "W1"),
                entry("CIN", "Cincinnati Bengals", 3, 2, "W1"),
                entry("CLE", "Cleveland Browns", 2, 3, "L2"),
            ]},
        }, {
            "name": "AFC West",
            "standings": {"entries": [
                entry("KC", "Kansas City Chiefs", 5, 0, "W5"),
                entry("LAC", "Los Angeles Chargers", 2, 3, "L1"),
                entry("DEN", "Denver Broncos", 2, 3, "W1"),
                entry("LV", "Las Vegas Raiders", 1, 4, "L3"),
            ]},
        }],
    }


# --------------------------------------------------------------- fantasy

def fantasy_league(week=2):
    """ESPN fantasy v3 payload, shaped as the real endpoint returns it."""
    def team(tid, abbrev, name, wins, losses, owner=None):
        raw = {"id": tid, "abbrev": abbrev, "name": name,
               "record": {"overall": {"wins": wins, "losses": losses,
                                      "ties": 0}}}
        if owner:
            raw["owners"] = [owner]
        return raw

    me = "{AABBCCDD-1234-5678-9012-ABCDEFABCDEF}"
    teams = [
        team(1, "HZ", "Zahir Zone", 2, 0, owner=me),
        team(2, "BRO", "Brotherly Shove", 1, 1),
        team(3, "SCA", "Scarlet Fever", 2, 0),
        team(4, "MID", "Midwest Mayhem", 0, 2),
        team(5, "BUCK", "Buckeye Bombers", 1, 1),
        team(6, "COL", "Columbus Crushers", 0, 2),
    ]

    def side(tid, total, live=None):
        node = {"teamId": tid, "totalPoints": total}
        if live is not None:
            node["rosterForCurrentScoringPeriod"] = {"appliedStatTotal": live}
        return node

    schedule = [
        {"matchupPeriodId": week, "home": side(1, 78.4, 91.2),
         "away": side(2, 84.6, 84.6)},
        {"matchupPeriodId": week, "home": side(3, 101.2), "away": side(4, 88.0)},
        {"matchupPeriodId": week, "home": side(5, 66.8), "away": side(6, 72.1)},
        # A previous week, which must be ignored.
        {"matchupPeriodId": week - 1, "home": side(1, 120.0),
         "away": side(3, 99.9)},
    ]

    return {
        "id": 123456,
        "scoringPeriodId": week,
        "seasonId": 2026,
        "status": {"currentMatchupPeriod": week},
        "settings": {"name": "Zahir Family League"},
        "teams": teams,
        "schedule": schedule,
    }
