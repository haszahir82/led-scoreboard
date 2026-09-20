"""Non-game screens: rankings, standings, leaders, team card, clock, weather.

These are what turn the panel from a scoreboard into something worth looking
at when nothing is playing, which on a college schedule is most of the week.
"""

import math
from datetime import datetime

from data import logos
from renderer import layout
from renderer.layout import FONTS
from .base import Screen, blink, scroll_x


class HeaderMixin:
    """Consistent title bar: label on the left, accent rule beneath."""

    def header(self, frame, title, right="", colour=layout.YELLOW):
        frame.text((1, 0), title, FONTS.small, colour)
        if right:
            frame.text_right((63, 0), right, FONTS.tiny, layout.DIM)
        frame.hline(7, fill=layout.DARK)


class RankingsScreen(Screen, HeaderMixin):
    """Three teams of the Top 25 per screen, paged over the screen's life."""

    kind = "info"
    ROWS = 3

    def __init__(self, ctx, league_key, page=0):
        super().__init__(ctx)
        self.league_key = league_key
        self.page = page
        self.key = "rankings:{}:{}".format(league_key, page)

    def draw(self, frame, elapsed):
        entries = self.ctx.store.get_rankings(self.league_key)
        poll = str(self.ctx.config.get(
            "leagues.{}.poll".format(self.league_key), "ap")).upper()
        self.header(frame, "{} TOP 25".format(poll),
                    right="{}-{}".format(self.page * self.ROWS + 1,
                                         min((self.page + 1) * self.ROWS, 25)))

        if not entries:
            frame.text_centered(14, "NO POLL YET", FONTS.small, layout.DIM)
            return

        start = self.page * self.ROWS
        rows = entries[start:start + self.ROWS]
        favourites = set(self.ctx.config.favorites(self.league_key))

        y = 9
        for entry in rows:
            self.draw_row(frame, y, entry, entry.abbr in favourites)
            y += 8

    def draw_row(self, frame, y, entry, is_favorite):
        rank = "{:>2}".format(entry.rank)
        frame.text((1, y), rank, FONTS.small,
                   layout.YELLOW if is_favorite else layout.DIM)

        icon = logos.get(entry.abbr, entry.logo_url, size=7,
                         league=self.league_key)
        if icon:
            frame.paste(icon, (10, y - 1))

        record_w = 0
        if entry.record:
            record_w = layout.text_width(frame.draw, entry.record, FONTS.tiny)
            frame.text_right((63, y), entry.record, FONTS.tiny, layout.DIM)

        # Abbreviations throughout, not nicknames. Nicknames only fit for some
        # teams, and a list that reads OSU / UGA / DUCKS is worse than one
        # that reads OSU / UGA / ORE.
        available = 63 - record_w - 2 - 18
        colour = layout.GREEN if is_favorite else layout.WHITE
        frame.text_fitted((18, y), entry.abbr.upper(), FONTS.tiny, colour,
                          available)


class StandingsScreen(Screen, HeaderMixin):
    """One division at a time, four teams per screen."""

    kind = "info"
    ROWS = 4

    def __init__(self, ctx, league_key, division, page=0):
        super().__init__(ctx)
        self.league_key = league_key
        self.division = division
        self.page = page
        self.key = "standings:{}:{}:{}".format(league_key, division, page)

    def draw(self, frame, elapsed):
        entries = [e for e in self.ctx.store.get_standings(self.league_key)
                   if e.division == self.division]
        entries.sort(key=lambda e: (-e.wins, e.losses))

        title = self.division.upper()
        title = title.replace("AMERICAN FOOTBALL CONFERENCE", "AFC")
        title = title.replace("NATIONAL FOOTBALL CONFERENCE", "NFC")
        self.header(frame, layout.fit(frame.draw, title, FONTS.small, 52),
                    colour=layout.CYAN)

        if not entries:
            frame.text_centered(14, "NO DATA", FONTS.small, layout.DIM)
            return

        favourites = set(self.ctx.config.favorites(self.league_key))
        rows = entries[self.page * self.ROWS:(self.page + 1) * self.ROWS]

        y = 9
        for entry in rows:
            fav = entry.abbr in favourites
            icon = logos.get(entry.abbr, entry.logo_url, size=6,
                             league=self.league_key)
            if icon:
                frame.paste(icon, (1, y - 1))
            frame.text((9, y), entry.abbr, FONTS.tiny,
                       layout.GREEN if fav else layout.WHITE)
            frame.text_right((45, y), entry.record, FONTS.tiny, layout.WHITE)
            if entry.streak:
                colour = layout.GREEN if entry.streak.startswith("W") else layout.RED
                frame.text_right((63, y), entry.streak, FONTS.tiny, colour)
            y += 6


class LeadersScreen(Screen, HeaderMixin):
    """Statistical leaders for one game."""

    kind = "info"

    def __init__(self, ctx, game):
        super().__init__(ctx)
        self.game = game
        self.key = "leaders:{}".format(game.id)

    def draw(self, frame, elapsed):
        game = self.game
        title = "{} {}-{} {}".format(game.away.short, game.away.score,
                                     game.home.score, game.home.short)
        self.header(frame, layout.fit(frame.draw, title, FONTS.small, 62),
                    colour=layout.GREEN if game.live else layout.DIM)

        leaders = game.leaders[:2]
        if not leaders:
            frame.text_centered(15, "NO STATS YET", FONTS.small, layout.DIM)
            return

        # Two leaders, two rows each. Squeezing three onto one row apiece left
        # about ten pixels for the player's name, which turned "HOWARD" into
        # "SH." -- the name is the whole point, so the third leader goes
        # instead of the name.
        y = 9
        for leader in leaders:
            frame.text((1, y), leader.category[:4], FONTS.tiny, layout.YELLOW)
            frame.text_fitted((18, y), leader.athlete, FONTS.tiny,
                              layout.WHITE, 45)
            frame.text_fitted((18, y + 6), self.condense(leader.value),
                              FONTS.tiny, layout.DIM, 45)
            y += 11

    @staticmethod
    def condense(value):
        """'312 YDS, 3 TD' -> '312YD 3TD'."""
        text = value.upper().replace(",", "")
        for long, short in (("YARDS", "YD"), ("YDS", "YD"), (" YD", "YD"),
                            (" TD", "TD"), (" INT", "INT"), (" REC", "REC"),
                            (" PTS", "PT"), (" AST", "AS"), (" REB", "RB")):
            text = text.replace(long, short)
        return " ".join(text.split())[:14]


class TeamCardScreen(Screen, HeaderMixin):
    """A favourite team's record and next or most recent game."""

    kind = "info"

    def __init__(self, ctx, league_key, abbr):
        super().__init__(ctx)
        self.league_key = league_key
        self.abbr = abbr
        self.key = "team:{}:{}".format(league_key, abbr)

    def _team_and_game(self):
        for game in self.ctx.store.snapshot():
            if game.league != self.league_key:
                continue
            for team in game.teams:
                if team.abbr == self.abbr:
                    return team, game
        return None, None

    def has_game(self):
        """Is this team on the slate at all?

        The playlist asks before putting the card in the rotation. A card for a
        team on a bye week has nothing to say, and a panel that spends four
        seconds announcing "CIN / NO GAME" is worse than one that simply moves
        on to something that is happening.
        """
        team, _ = self._team_and_game()
        return team is not None

    def draw(self, frame, elapsed):
        team, game = self._team_and_game()
        if team is None:
            # The playlist filters these out, so reaching here means the slate
            # changed between the rebuild and this frame -- at most a few
            # seconds. Show the team, not an announcement about its absence.
            frame.text_centered(13, self.abbr, FONTS.small, layout.WHITE)
            return

        nfl = self.league_key == "nfl"
        # require_legible=False on purpose. For an arbitrary MAC team the
        # legibility gate is right: a mark that will not read at 16px is worse
        # than four clear letters. But this screen exists BECAUSE the team is a
        # favourite, and the person who chose it wants to see their logo, not
        # their abbreviation. The dark-logo boost handles the dim cases now.
        icon = logos.get(team.abbr, team.logo_url, size=16,
                         helmet=self.ctx.helmet_logos and nfl,
                         league=self.league_key, require_legible=False)
        frame.logo_or_abbr(icon, (1, 1), team.short, 16,
                           layout.hex_color(team.color))

        # Reserve the rank badge's width before fitting the name, or a ranked
        # team's name runs straight underneath its own ranking.
        rank_w = 0
        if team.rank:
            badge = "#{}".format(team.rank)
            rank_w = layout.text_width(frame.draw, badge, FONTS.tiny) + 2
            frame.text_right((63, 2), badge, FONTS.tiny, layout.RANK)

        name = (team.location or team.display).upper()
        frame.text_fitted((20, 2), name, FONTS.small, layout.WHITE,
                          43 - rank_w)

        if team.record:
            frame.text((20, 10), team.record, FONTS.small, layout.YELLOW)
        if team.conf_record:
            frame.text((44, 11), team.conf_record + "C", FONTS.tiny, layout.DIM)

        frame.hline(19, fill=layout.DARK)

        other = game.home if game.away.abbr == team.abbr else game.away
        # Home team hosts, so the opponent is the one they play "VS".
        prefix = "VS" if team.home else "AT"
        if game.live:
            line = "LIVE {} {}-{}".format(game.period_label, game.away.score,
                                          game.home.score)
            colour = layout.GREEN
        elif game.final:
            won = team.winner
            line = "{} {}-{}".format("W" if won else "L",
                                     max(game.away.score, game.home.score),
                                     min(game.away.score, game.home.score))
            line += " {} {}".format(prefix, other.short)
            colour = layout.GREEN if won else layout.RED
        else:
            local = self.to_local(game.start)
            when = local.strftime("%a %-I%p").upper() if local else "TBD"
            line = "{} {} {}".format(prefix, other.short, when)
            colour = layout.DIM

        frame.text_fitted((1, 22), line, FONTS.tiny, colour, 62)


class ClockScreen(Screen):
    kind = "info"
    key = "clock"

    def draw(self, frame, elapsed):
        now = self.now_local()
        fmt24 = self.ctx.config.get("display.time_format", "12h") == "24h"
        time_str = now.strftime("%H:%M") if fmt24 else now.strftime("%-I:%M")

        frame.text_centered(4, time_str, FONTS.medium, layout.WHITE)
        if not fmt24:
            width = layout.text_width(frame.draw, time_str, FONTS.medium)
            frame.text(((self.width + width) // 2 + 1, 5),
                       now.strftime("%p"), FONTS.tiny, layout.DIM)

        frame.hline(20, fill=layout.DARK)
        frame.text_centered(23, now.strftime("%a %b %-d").upper(),
                            FONTS.small, layout.YELLOW)


class WeatherScreen(Screen):
    kind = "info"
    key = "weather"

    def draw(self, frame, elapsed):
        weather = self.ctx.store.get_weather()
        if not weather:
            frame.text_centered(12, "NO WEATHER", FONTS.small, layout.DIM)
            return

        self.draw_icon(frame, 2, 4, weather.icon, elapsed)

        # The pixel fonts have no usable degree glyph (chr(176) renders as a
        # filled box, which reads as another zero), so it is drawn by hand.
        temp = str(weather.temp)
        frame.text((22, 3), temp, FONTS.medium, layout.WHITE)
        deg_x = 22 + layout.text_width(frame.draw, temp, FONTS.medium) + 2
        frame.draw.rectangle([deg_x, 3, deg_x + 2, 5], outline=layout.WHITE)

        frame.text((22, 15), weather.text[:11], FONTS.tiny, layout.DIM)

        # City name, now that the ZIP lookup gives us one for free.
        location = getattr(self.ctx.store, "location", None)
        if location and location.city:
            frame.text_fitted((22, 21), location.city.upper(), FONTS.tiny,
                              layout.DARK, 41)

        frame.hline(26, fill=layout.DARK)
        frame.text((1, 27), "H {}".format(weather.high), FONTS.tiny, layout.RED)
        frame.text((22, 27), "L {}".format(weather.low), FONTS.tiny, layout.CYAN)
        frame.text_right((63, 27), "{}MPH".format(weather.wind), FONTS.tiny,
                         layout.DIM)

    def draw_icon(self, frame, x, y, icon, elapsed):
        """Hand-drawn 16x16 glyphs. Icon fonts do not survive this scale."""
        if icon in ("sun", "partly"):
            cx, cy, r = x + 7, y + 7, 3
            frame.draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=layout.YELLOW)
            for i in range(8):
                angle = math.pi / 4 * i + (elapsed * 0.4)
                x0 = cx + math.cos(angle) * (r + 2)
                y0 = cy + math.sin(angle) * (r + 2)
                x1 = cx + math.cos(angle) * (r + 4)
                y1 = cy + math.sin(angle) * (r + 4)
                frame.draw.line([(x0, y0), (x1, y1)], fill=layout.YELLOW)
        if icon in ("partly", "cloud", "fog", "rain", "drizzle", "snow", "storm"):
            colour = layout.DIM if icon != "storm" else (170, 170, 190)
            frame.draw.ellipse([x + 1, y + 6, x + 9, y + 12], fill=colour)
            frame.draw.ellipse([x + 5, y + 4, x + 14, y + 12], fill=colour)
            frame.draw.rectangle([x + 3, y + 9, x + 13, y + 12], fill=colour)
        drop_colour = {"rain": layout.CYAN, "drizzle": layout.CYAN,
                       "snow": layout.WHITE, "storm": layout.YELLOW}.get(icon)
        if drop_colour:
            phase = int(elapsed * 8) % 4
            for i, dx in enumerate((3, 7, 11)):
                dy = y + 13 + ((phase + i) % 3)
                if icon == "storm" and i == 1:
                    frame.draw.line([(x + dx, y + 13), (x + dx - 2, y + 16),
                                     (x + dx + 1, y + 16)], fill=layout.YELLOW)
                else:
                    frame.pixel(x + dx, dy, drop_colour)
                    if icon != "snow":
                        frame.pixel(x + dx, dy + 1, drop_colour)


class TickerScreen(Screen):
    """Every score on one scrolling line, the way a TV bug does it."""

    kind = "info"
    key = "ticker"

    def __init__(self, ctx, games):
        super().__init__(ctx)
        self.games = games
        self._built = None      # (segments, widths, total), built once

    @property
    def duration(self):
        # Long enough for one full pass, capped so it cannot hijack the loop.
        return min(45.0, max(12.0, len(self.games) * 3.0))

    def _layout_cache(self, draw):
        """Build the strip once, not once per frame.

        The naive version reformatted every score and called textbbox on each
        one on every frame. At 10fps with 14 games that is 140 text
        measurements a second, which is real work on a single ARMv6 core and
        was by far the most expensive screen in the rotation. The strip only
        changes when the data does, and a new screen object is constructed on
        every rebuild, so caching on the instance is sufficient and needs no
        invalidation logic.
        """
        if self._built is None:
            segments = self._segments()
            gap = 8
            widths = [layout.text_width(draw, text, FONTS.small) + gap
                      for text, _ in segments]
            self._built = (segments, widths, sum(widths))
        return self._built

    def _segments(self):
        """'OSU 21-17 MICH Q3' style entries.

        Ranks are left out here on purpose. In a scrolling line a leading
        number reads as part of the score, so "2 OSU 21 6 MICH 17" is four
        numbers in a row and parses as nothing at all.
        """
        out = []
        for game in self.games:
            away, home = game.away.short, game.home.short
            if game.live:
                text = "{} {}-{} {} {}".format(away, game.away.score,
                                               game.home.score, home,
                                               game.period_label or "")
                colour = layout.GREEN
            elif game.final:
                text = "{} {}-{} {} F".format(away, game.away.score,
                                              game.home.score, home)
                colour = layout.WHITE
            else:
                local = self.to_local(game.start)
                when = local.strftime("%-I:%M").lstrip("0") if local else "TBD"
                text = "{} {} {}".format(away, home, when)
                colour = layout.DIM
            out.append((" ".join(text.split()), colour))
        return out

    def draw(self, frame, elapsed):
        frame.text((1, 0), "SCORES", FONTS.small, layout.YELLOW)
        live = sum(1 for g in self.games if g.live)
        if live:
            frame.text_right((63, 0), "{} LIVE".format(live), FONTS.tiny,
                             layout.GREEN)
        frame.hline(7, fill=layout.DARK)

        segments, widths, total = self._layout_cache(frame.draw)
        if not segments:
            frame.text_centered(14, "NO GAMES", FONTS.small, layout.DIM)
            return

        speed = 1.0 / max(self.ctx.config.get("display.scroll_speed", 0.04), 0.005)
        offset = int(elapsed * speed) % total if total else 0

        # Walk to the first segment that is actually on screen instead of
        # calling draw.text for every game and letting Pillow clip the ones
        # that are off-panel. Only two or three of them are ever visible.
        count = len(segments)
        index, x = 0, -offset
        while index < count and x + widths[index] <= 0:
            x += widths[index]
            index += 1

        drawn = 0
        while x < self.width and drawn < count:
            text, colour = segments[index % count]
            frame.text((x, 12), text, FONTS.small, colour)
            x += widths[index % count]
            index += 1
            drawn += 1


class MessageScreen(Screen):
    """Offseason, startup, and 'cannot reach ESPN' states."""

    kind = "info"

    def __init__(self, ctx, title, subtitle="", colour=layout.YELLOW, key="message"):
        super().__init__(ctx)
        self.title = title
        self.subtitle = subtitle
        self.colour = colour
        self.key = key

    def draw(self, frame, elapsed):
        frame.text_centered(8, self.title, FONTS.small, self.colour)
        if self.subtitle:
            frame.text_centered(18, self.subtitle, FONTS.tiny, layout.DIM)
