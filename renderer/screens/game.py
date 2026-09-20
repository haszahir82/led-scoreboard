"""Pregame, live and final game screens.

Shared geometry on a 64x32 panel:

    x 0..19    away logo          y 0..19
    x 21..43   centre column      y 0..19   (23px: state, clock, possession)
    x 44..63   home logo          y 0..19
    y 20..31   bottom band                  (scores outside, situation inside)

The bottom band is where the three states differ most: pregame shows kickoff
time and betting line, live shows down/distance and field position, final
shows the score with the winner highlighted.
"""

from datetime import datetime, timezone

from renderer import layout
from renderer.layout import FONTS
from .base import GameScreen, blink, scroll_x

# Bottom band geometry, shared by all three states.
BAND_RULE = 19
BAND_TOP = 20          # big score numerals sit here (11px tall -> rows 20..30)
LINE_1 = 20
LINE_2 = 26


def split_down(text):
    """'3rd & 7' -> ('3RD', '7'); '2nd & Goal' -> ('2ND', 'GOAL').

    The ampersand is dropped rather than abbreviated because none of the
    bundled pixel fonts render '&' legibly at 5px: it comes out as an 'S' or a
    '6', so "3RD&7" reads as "3RDS7" or "3RD67". Colouring the two halves
    differently carries the same meaning with no glyph at all.
    """
    upper = text.upper().replace("&", " ")
    parts = [p for p in upper.split() if p]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return (parts[0] if parts else ""), ""


class LiveGameScreen(GameScreen):
    kind = "live"

    def draw(self, frame, elapsed):
        game = self.game
        self.draw_logos(frame)

        # ---- centre column: period, clock, possession ----
        period = game.period_label or "LIVE"
        if game.halftime:
            self.center_text(frame, 1, "HALF", FONTS.small, layout.YELLOW)
        else:
            self.center_text(frame, 1, period, FONTS.small, layout.LIVE)
            clock = game.clock or ""
            if clock:
                self.center_text(frame, 8, clock, FONTS.small, layout.WHITE)

        self.draw_possession(frame, elapsed)

        # ---- bottom band ----
        frame.hline(BAND_RULE, fill=layout.DARK)
        self.draw_scores(frame, y=BAND_TOP)
        self.draw_situation(frame, elapsed)

    def draw_possession(self, frame, elapsed):
        """Who has the ball, as a coloured wedge pointing at their side.

        A wedge beats printing the abbreviation again: it reads instantly from
        across a room and costs three pixels of width.
        """
        game = self.game
        if not game.possession_id:
            return
        colour = layout.REDZONE if game.red_zone else layout.POSSESSION
        if game.red_zone and not blink(elapsed, period=1.0, duty=0.65):
            return

        y = 15
        if game.has_possession(game.away):
            x = self.CENTER_X0 + 1
            for i in range(4):
                frame.draw.line([(x + i, y + i), (x + i, y + 4 - i)], fill=colour)
        elif game.has_possession(game.home):
            x = self.CENTER_X1 - 4
            for i in range(4):
                frame.draw.line([(x + 3 - i, y + i), (x + 3 - i, y + 4 - i)],
                                fill=colour)

    def draw_scores(self, frame, y):
        game = self.game
        away = str(game.away.score)
        home = str(game.home.score)
        frame.text((1, y), away, FONTS.score, layout.WHITE)
        frame.text_right((63, y), home, FONTS.score, layout.WHITE)

    def draw_situation(self, frame, elapsed):
        """Down & distance over field position, in the gap between scores."""
        game = self.game
        away_w = layout.text_width(frame.draw, str(game.away.score), FONTS.score)
        home_w = layout.text_width(frame.draw, str(game.home.score), FONTS.score)
        x0 = 1 + away_w + 2
        x1 = 63 - home_w - 2
        span = max(0, x1 - x0)

        if not game.down_distance and not game.yard_line:
            # Non-football, or football between drives: show the broadcast or
            # the matchup so the band is never empty.
            filler = game.broadcast.upper() or game.matchup
            frame.text_centered(23, layout.fit(frame.draw, filler, FONTS.tiny, span),
                                FONTS.tiny, layout.DIM, x0=x0, x1=x1)
            return

        two_lines = bool(game.down_distance and game.yard_line)
        if game.down_distance:
            self.draw_down(frame, LINE_1 if two_lines else 23, x0, x1)
        if game.yard_line:
            yard = game.yard_line.upper().replace(" ", "")
            frame.text_centered(LINE_2 if two_lines else 23,
                                layout.fit(frame.draw, yard, FONTS.tiny, span),
                                FONTS.tiny, layout.DIM, x0=x0, x1=x1)

    def draw_down(self, frame, y, x0, x1):
        """Down in white (red in the red zone), distance in yellow."""
        down, distance = split_down(self.game.down_distance)
        down_colour = layout.REDZONE if self.game.red_zone else layout.WHITE
        span = max(0, x1 - x0)

        gap = 3
        down_w = layout.text_width(frame.draw, down, FONTS.tiny)
        dist_w = layout.text_width(frame.draw, distance, FONTS.tiny)
        total = down_w + (gap + dist_w if distance else 0)

        if total > span and distance:
            # No room for both: the distance is the part that changes, so it
            # is the part worth keeping.
            distance = layout.fit(frame.draw, distance, FONTS.tiny, span)
            frame.text_centered(y, distance, FONTS.tiny, layout.YELLOW,
                                x0=x0, x1=x1)
            return

        x = x0 + max(0, (span - total) // 2)
        frame.text((x, y), down, FONTS.tiny, down_colour)
        if distance:
            frame.text((x + down_w + gap, y), distance, FONTS.tiny, layout.YELLOW)


class PregameScreen(GameScreen):
    kind = "pregame"

    def draw(self, frame, elapsed):
        game = self.game
        self.draw_logos(frame)

        # ---- centre: countdown inside an hour, otherwise VS/@ ----
        remaining = game.starts_in()
        if remaining and remaining.total_seconds() < 3600:
            self.center_text(frame, 1, "IN", FONTS.tiny, layout.DIM)
            total = int(remaining.total_seconds())
            self.center_text(frame, 7, "{}:{:02d}".format(total // 60, total % 60),
                             FONTS.small, layout.YELLOW)
        else:
            self.center_text(frame, 2, self.day_string(), FONTS.tiny, layout.DIM)
            self.center_text(frame, 8, self.time_string(), FONTS.small, layout.WHITE)

        # The centre column's third row: the network if there is one, else a
        # neutral-site marker. Records used to live down here, but the logos
        # occupy the full 20px above the divider, so anything on this row
        # would be drawn over them.
        if game.broadcast:
            self.center_text(frame, 14, game.broadcast.upper()[:5], FONTS.tiny,
                             layout.DIM)
        elif game.neutral_site:
            self.center_text(frame, 14, "N", FONTS.tiny, layout.DIM)

        frame.hline(BAND_RULE, fill=layout.DARK)
        self.draw_context(frame, 21, elapsed)

    def draw_context(self, frame, y, elapsed):
        """The betting line, at a size that reads from across a room.

        This used to be drawn in the 5px font at the very bottom of the panel.
        At that height the glyphs lose their strokes, so it does not read as
        small text, it reads as broken text, which is why it looked cut off.

        The 9px numerals are nearly twice the height and are the smallest
        thing here that is genuinely legible at distance. The band is 12px, so
        exactly one row of them fits, which means the band shows the line and
        nothing else. Records moved up under the logos to pay for it.
        """
        game = self.game
        spread = (game.odds or "").upper()
        total = (game.over_under or "").upper()
        font = FONTS.score_small
        avail = self.width - 2

        if not spread and not total:
            # No line published. Fill the band with the matchup rather than
            # leaving a third of the panel empty. The score font has no "@"
            # glyph and renders it as an empty box, so use a dash.
            matchup = "{}-{}".format(game.away.short, game.home.short)
            frame.text_centered(y, layout.fit(frame.draw, matchup, font, avail),
                                font, layout.DIM)
            return

        def measure(a, b):
            wa = layout.text_width(frame.draw, a, font) if a else 0
            wb = layout.text_width(frame.draw, b, font) if b else 0
            return wa + wb + (4 if a and b else 0)

        # Shed the least informative characters first, keeping the large font
        # throughout: "O/U 41.5" means the same as "41.5" once it is the only
        # thing on the right, and the favourite's abbreviation is already on
        # screen above the spread.
        if measure(spread, total) > avail and total.startswith("O/U"):
            total = total.replace("O/U", "").strip()
        if measure(spread, total) > avail and " " in spread:
            spread = spread.split(" ", 1)[1]
        if measure(spread, total) > avail:
            total = ""
            spread = layout.fit(frame.draw, spread, font, avail)

        if spread and total:
            frame.text((1, y), spread, font, layout.YELLOW)
            frame.text_right((63, y), total, font, layout.DIM)
        else:
            # One value alone gets centred rather than stranded on an edge.
            frame.text_centered(y, spread or total, font, layout.YELLOW)


class FinalScreen(GameScreen):
    kind = "final"

    def draw(self, frame, elapsed):
        game = self.game
        self.draw_logos(frame)

        label = "FINAL"
        detail = (game.detail or "").upper()
        if "OT" in detail:
            label = "F/OT"
        self.center_text(frame, 2, label, FONTS.small, layout.DIM)

        # Winner gets a small chevron in the centre column pointing their way.
        if game.away.winner or game.home.winner:
            colour = layout.GREEN
            y = 11
            if game.away.winner:
                x = self.CENTER_X0 + 2
                for i in range(4):
                    frame.draw.line([(x + i, y + i), (x + i, y + 4 - i)], fill=colour)
            else:
                x = self.CENTER_X1 - 5
                for i in range(4):
                    frame.draw.line([(x + 3 - i, y + i), (x + 3 - i, y + 4 - i)],
                                    fill=colour)

        frame.hline(BAND_RULE, fill=layout.DARK)

        away = str(game.away.score)
        home = str(game.home.score)
        frame.text((1, BAND_TOP), away, FONTS.score,
                   layout.WHITE if game.away.winner else layout.DIM)
        frame.text_right((63, BAND_TOP), home, FONTS.score,
                         layout.WHITE if game.home.winner else layout.DIM)

        away_w = layout.text_width(frame.draw, away, FONTS.score)
        home_w = layout.text_width(frame.draw, home, FONTS.score)
        x0, x1 = 1 + away_w + 2, 63 - home_w - 2
        span = max(0, x1 - x0)

        # Prefer the recap headline; fall back to the day when it will not fit
        # rather than showing three truncated words.
        note = (game.headline or "").upper()
        if not note or layout.text_width(frame.draw, note, FONTS.tiny) > span:
            note = self.day_string()
        frame.text_centered(23, layout.fit(frame.draw, note, FONTS.tiny, span),
                            FONTS.tiny, layout.DIM, x0=x0, x1=x1)


def for_game(ctx, game):
    """Pick the right screen class for a game's current state."""
    if game.live:
        return LiveGameScreen(ctx, game)
    if game.final:
        return FinalScreen(ctx, game)
    return PregameScreen(ctx, game)
