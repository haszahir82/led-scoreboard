"""Fantasy screens: your matchup, and where you sit in the league.

The matchup screen is the one that matters. It answers "am I winning right
now", which is the only question anyone actually has on a Sunday afternoon,
and it answers it from across a room: your points, their points, and the
margin in a colour that says which way it is going.
"""

from renderer import layout
from renderer.layout import FONTS
from .base import Screen, blink, scroll_x


class FantasyMatchupScreen(Screen):
    """My team vs my opponent this week."""

    kind = "info"

    def __init__(self, ctx, matchup):
        super().__init__(ctx)
        self.matchup = matchup
        self.key = "fantasy:matchup:{}:{}".format(
            matchup.week, matchup.mine.id if matchup.has_mine else "x")

    def draw(self, frame, elapsed):
        m = self.matchup
        mine = m.mine if m.has_mine else m.away
        theirs = m.theirs if m.has_mine else m.home
        ahead = mine.points >= theirs.points

        # ---- header: week left, league name centred ----
        frame.text((1, 0), "W{}".format(m.week), FONTS.tiny, layout.YELLOW)
        name = (m.league_name or "FANTASY").upper()
        frame.text_centered(0, layout.fit(frame.draw, name, FONTS.tiny, 44),
                            FONTS.tiny, layout.DIM, x0=13, x1=63)
        frame.hline(6, fill=layout.DARK)

        # ---- who ----
        colour = layout.GREEN if ahead else layout.RED
        frame.text_fitted((1, 8), mine.short, FONTS.small, colour, 28)
        frame.text_right((63, 8), theirs.short, FONTS.small, layout.WHITE)

        # ---- records, with the margin in the gap between them ----
        # The margin lives on this row rather than beside the points: three
        # digits each side plus a decimal leaves no centre column down there,
        # and a "+106.8" printed over the scores is worse than no margin.
        my_rec = mine.record
        their_rec = theirs.record
        frame.text((1, 14), my_rec, FONTS.tiny, layout.DIM)
        frame.text_right((63, 14), their_rec, FONTS.tiny, layout.DIM)

        margin = abs(mine.points - theirs.points)
        label = "{}{:.1f}".format("+" if ahead else "-", margin)
        x0 = 1 + layout.text_width(frame.draw, my_rec, FONTS.tiny) + 3
        x1 = 63 - layout.text_width(frame.draw, their_rec, FONTS.tiny) - 3
        if x1 - x0 >= layout.text_width(frame.draw, label, FONTS.tiny):
            frame.text_centered(14, label, FONTS.tiny, colour, x0=x0, x1=x1)

        # ---- points, the reason this screen exists ----
        frame.text((1, 21), self._points(mine.points), FONTS.score_small,
                   colour)
        frame.text_right((63, 21), self._points(theirs.points),
                         FONTS.score_small, layout.WHITE)

    @staticmethod
    def _points(value):
        """87.4, or 104 once three digits leave no room for a decimal."""
        return "{:.0f}".format(value) if value >= 100 else "{:.1f}".format(value)


class FantasyLeagueScreen(Screen):
    """Standings, four teams at a time."""

    kind = "info"
    ROWS = 4

    def __init__(self, ctx, state, page=0):
        super().__init__(ctx)
        self.state = state
        self.page = page
        self.key = "fantasy:league:{}".format(page)

    def draw(self, frame, elapsed):
        name = (self.state.name or "FANTASY").upper()
        frame.text((1, 0), layout.fit(frame.draw, name, FONTS.small, 44),
                   FONTS.small, layout.YELLOW)
        frame.text_right((63, 0), "W{}".format(self.state.week), FONTS.tiny,
                         layout.DIM)
        frame.hline(7, fill=layout.DARK)

        teams = self.state.teams[self.page * self.ROWS:(self.page + 1) * self.ROWS]
        if not teams:
            frame.text_centered(14, "NO TEAMS", FONTS.small, layout.DIM)
            return

        y = 9
        for index, team in enumerate(teams):
            rank = self.page * self.ROWS + index + 1
            colour = layout.GREEN if team.is_mine else layout.WHITE
            frame.text((1, y), "{:>2}".format(rank), FONTS.tiny,
                       layout.YELLOW if team.is_mine else layout.DARK)
            frame.text_fitted((11, y), team.short, FONTS.tiny, colour, 30)
            frame.text_right((63, y), team.record, FONTS.tiny, layout.DIM)
            y += 6


class FantasyAuthScreen(Screen):
    """Shown when a private league's saved login has stopped working.

    A silent failure would just look like the fantasy screens disappearing,
    which is the kind of thing that goes unnoticed for a month. This says what
    is wrong and where to fix it.
    """

    kind = "info"
    key = "fantasy:auth"

    def __init__(self, ctx, host=""):
        super().__init__(ctx)
        self.host = host

    def draw(self, frame, elapsed):
        frame.text_centered(1, "FANTASY", FONTS.small, layout.YELLOW)
        frame.hline(9, fill=layout.DARK)
        if blink(elapsed, period=2.0, duty=0.75):
            frame.text_centered(12, "SIGN IN AGAIN", FONTS.tiny, layout.RED)
        where = self.host or "the settings page"
        frame.text_centered(20, "OPEN", FONTS.tiny, layout.DIM)
        frame.text_fitted((1, 26), where.upper(), FONTS.tiny, layout.DIM,
                          frame.width - 2)
