"""Normalized game/team model.

The renderer only ever sees these objects, never raw ESPN JSON. That keeps
layout code free of `.get('competitions')[0]...` chains and means a second
data source could be dropped in later without touching a single screen.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class Team:
    id: str = ""
    abbr: str = ""
    name: str = ""            # "Buckeyes"
    location: str = ""        # "Ohio State"
    display: str = ""         # "Ohio State Buckeyes"
    score: int = 0
    rank: Optional[int] = None      # curatedRank, 1-25, else None
    record: str = ""                # "3-1"
    conf_record: str = ""           # "1-0"
    color: str = "ffffff"
    alt_color: str = "000000"
    logo_url: str = ""
    winner: bool = False
    home: bool = False
    # Baseball / basketball extras
    linescores: List[int] = field(default_factory=list)

    @property
    def short(self) -> str:
        return self.abbr or self.location[:4].upper()

    @property
    def ranked_short(self) -> str:
        if self.rank and self.rank <= 25:
            return "{} {}".format(self.rank, self.short)
        return self.short


@dataclass
class Leader:
    """A statistical leader line, e.g. 'PASS  BURROW 312 YD 3 TD'."""
    category: str = ""        # "PASS"
    athlete: str = ""         # "BURROW"
    value: str = ""           # "312 YD, 3 TD"
    team_abbr: str = ""


@dataclass
class Game:
    id: str = ""
    league: str = "nfl"
    league_label: str = "NFL"
    start: Optional[datetime] = None       # tz-aware UTC
    state: str = "pre"                     # pre | in | post
    detail: str = ""                       # "Final/OT", "Halftime", "9:31 - 2nd"
    clock: str = ""                        # "9:31"
    period: int = 0
    period_label: str = ""                 # "Q2", "T7", "HALF"
    home: Team = field(default_factory=Team)
    away: Team = field(default_factory=Team)
    # Football situation
    down_distance: str = ""                # "3rd & 7"
    yard_line: str = ""                    # "CIN 42"
    possession_id: str = ""
    red_zone: bool = False
    # Context
    broadcast: str = ""                    # "CBS"
    venue: str = ""
    odds: str = ""                         # "OSU -7.5"
    over_under: str = ""
    headline: str = ""                     # ESPN recap headline, post games
    leaders: List[Leader] = field(default_factory=list)
    neutral_site: bool = False
    # Set by the store, not the parser
    is_favorite: bool = False
    is_ranked_matchup: bool = False

    # ---------- state helpers ----------

    @property
    def live(self) -> bool:
        return self.state == "in"

    @property
    def final(self) -> bool:
        return self.state == "post"

    @property
    def pregame(self) -> bool:
        return self.state == "pre"

    @property
    def halftime(self) -> bool:
        return "halftime" in self.detail.lower() or self.period_label == "HALF"

    @property
    def teams(self):
        return (self.away, self.home)

    @property
    def matchup(self) -> str:
        return "{} @ {}".format(self.away.short, self.home.short)

    @property
    def best_rank(self) -> int:
        """Lowest (best) AP rank in the game; 99 when neither side is ranked."""
        ranks = [t.rank for t in self.teams if t.rank]
        return min(ranks) if ranks else 99

    @property
    def total_score(self) -> int:
        return self.home.score + self.away.score

    @property
    def margin(self) -> int:
        return abs(self.home.score - self.away.score)

    def has_possession(self, team: Team) -> bool:
        return bool(self.possession_id) and self.possession_id == team.id

    def starts_in(self, now: Optional[datetime] = None):
        if not self.start:
            return None
        now = now or datetime.now(timezone.utc)
        return self.start - now

    def involves(self, abbrs) -> bool:
        wanted = {a.upper() for a in abbrs}
        return bool(wanted & {self.home.abbr.upper(), self.away.abbr.upper()})

    def __repr__(self):
        return "<Game {} {} {}-{} {}>".format(
            self.league, self.matchup, self.away.score, self.home.score, self.state)


@dataclass
class RankingEntry:
    rank: int
    abbr: str
    name: str
    record: str = ""
    points: str = ""
    trend: str = ""          # "+2", "-1", "-"
    logo_url: str = ""


@dataclass
class StandingEntry:
    abbr: str
    name: str
    wins: int = 0
    losses: int = 0
    ties: int = 0
    pct: str = ""
    games_back: str = ""
    streak: str = ""
    division: str = ""
    logo_url: str = ""

    @property
    def record(self) -> str:
        if self.ties:
            return "{}-{}-{}".format(self.wins, self.losses, self.ties)
        return "{}-{}".format(self.wins, self.losses)
