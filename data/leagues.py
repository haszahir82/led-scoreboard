"""Registry of every league the scoreboard knows how to talk to.

Every ESPN "site API" scoreboard endpoint returns the same document shape
regardless of sport, so one generic parser (see espn.py) handles all of them.
The only per-league differences are the URL path, how a period is named
("Q3" vs "T7" vs "P2"), and which extra situational fields are worth showing.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


def _football_period(period: int, state: str, detail: str) -> str:
    if period == 0:
        return ""
    if period > 4:
        return "OT" if period == 5 else "{}OT".format(period - 4)
    if "halftime" in detail.lower():
        return "HALF"
    return "Q{}".format(period)


def _basketball_period(period: int, state: str, detail: str) -> str:
    if period == 0:
        return ""
    if period > 4:
        return "OT" if period == 5 else "{}OT".format(period - 4)
    if "half" in detail.lower():
        return "HALF"
    return "Q{}".format(period)


def _college_basketball_period(period: int, state: str, detail: str) -> str:
    if period == 0:
        return ""
    if period > 2:
        return "OT" if period == 3 else "{}OT".format(period - 2)
    return "1H" if period == 1 else "2H"


def _hockey_period(period: int, state: str, detail: str) -> str:
    if period == 0:
        return ""
    if period == 4:
        return "OT"
    if period > 4:
        return "SO"
    return "P{}".format(period)


def _baseball_period(period: int, state: str, detail: str) -> str:
    d = detail.lower()
    if "top" in d:
        return "T{}".format(period)
    if "bot" in d or "mid" in d or "end" in d:
        return "B{}".format(period)
    return str(period) if period else ""


def _soccer_period(period: int, state: str, detail: str) -> str:
    # detail is usually the live clock already, e.g. "63'"
    return detail.split(" ")[0][:5] if detail else ""


@dataclass
class League:
    key: str                      # config key, e.g. "nfl"
    label: str                    # short label for the display, e.g. "NFL"
    path: str                     # ESPN sport/league path
    period_fn: Callable           # (period, state, detail) -> short period label
    # Extra querystring appended to the scoreboard call. groups=80 restricts
    # college football to FBS, which is what "Top 25" means in practice.
    query: str = ""
    ranked: bool = False          # league publishes a Top 25 (curatedRank)
    downs: bool = False           # show down & distance / possession
    rankings_path: Optional[str] = None
    standings_path: Optional[str] = None
    # A scoreboard call only returns "today". For sports with a weekly
    # schedule we widen the window so Saturday's slate shows up on Thursday.
    lookahead_days: int = 0


LEAGUES: Dict[str, League] = {
    "nfl": League(
        key="nfl", label="NFL", path="football/nfl",
        period_fn=_football_period, downs=True,
        standings_path="football/nfl",
    ),
    "ncaaf": League(
        key="ncaaf", label="CFB", path="football/college-football",
        period_fn=_football_period, query="groups=80&limit=400",
        ranked=True, downs=True,
        rankings_path="football/college-football",
        standings_path="football/college-football",
        lookahead_days=1,
    ),
    "nba": League(
        key="nba", label="NBA", path="basketball/nba",
        period_fn=_basketball_period,
        standings_path="basketball/nba",
    ),
    "ncaam": League(
        key="ncaam", label="CBB", path="basketball/mens-college-basketball",
        period_fn=_college_basketball_period, query="groups=50&limit=400",
        ranked=True,
        rankings_path="basketball/mens-college-basketball",
    ),
    "nhl": League(
        key="nhl", label="NHL", path="hockey/nhl",
        period_fn=_hockey_period,
        standings_path="hockey/nhl",
    ),
    "mlb": League(
        key="mlb", label="MLB", path="baseball/mlb",
        period_fn=_baseball_period,
        standings_path="baseball/mlb",
    ),
    "wnba": League(
        key="wnba", label="WNBA", path="basketball/wnba",
        period_fn=_basketball_period,
    ),
    "epl": League(
        key="epl", label="EPL", path="soccer/eng.1",
        period_fn=_soccer_period,
    ),
    "ucl": League(
        key="ucl", label="UCL", path="soccer/uefa.champions",
        period_fn=_soccer_period,
    ),
    "mls": League(
        key="mls", label="MLS", path="soccer/usa.1",
        period_fn=_soccer_period,
    ),
}


def get(key: str) -> Optional[League]:
    return LEAGUES.get(key.lower())


def all_keys() -> List[str]:
    return list(LEAGUES.keys())
