"""Screen base class and shared game-screen furniture."""

from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo          # Python 3.9+
except ImportError:
    ZoneInfo = None

try:
    import pytz                            # fallback for Python 3.7 / 3.8
except ImportError:
    pytz = None

from data import logos
from renderer import layout
from renderer.layout import FONTS, Frame

_tz_cache = {}


def resolve_tz(name):
    """IANA name -> tzinfo, or None to mean 'use the system timezone'.

    Older Pi OS images ship Python 3.7, which predates zoneinfo, so pytz is
    used when it is available. If neither works the result is None and the
    board falls back to system local time: a typo or a missing library should
    show slightly wrong times, not take the panel down.
    """
    if not name:
        return None
    if name in _tz_cache:
        return _tz_cache[name]

    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(name)
        except Exception:
            tz = None
    if tz is None and pytz is not None:
        try:
            # astimezone() calls fromutc(), which pytz handles correctly. The
            # pytz localize() caveat only applies to naive datetimes, and
            # every datetime here is already UTC-aware from the parser.
            tz = pytz.timezone(name)
        except Exception:
            tz = None

    _tz_cache[name] = tz
    return tz


class Screen:
    """One thing the panel shows for a while.

    `draw` is called every frame with the elapsed seconds since the screen
    became active, so animation and scrolling are simply functions of time
    rather than of a frame counter that drifts.
    """

    kind = "info"          # picks the rotation rate: live | pregame | final | info
    key = "screen"         # stable identity, used to avoid restarting a screen
    is_info = True         # False for game screens; drives the rotation mix

    def __init__(self, ctx):
        self.ctx = ctx     # RenderContext: config, store, panel size

    @property
    def duration(self):
        return self.ctx.config.rate(self.kind)

    def draw(self, frame, elapsed):
        raise NotImplementedError

    # ---- helpers available to every screen ----

    @property
    def width(self):
        return self.ctx.width

    @property
    def height(self):
        return self.ctx.height

    # ---- time ----

    @property
    def tz(self):
        return resolve_tz(self.ctx.config.get("display.timezone", ""))

    def to_local(self, dt):
        """UTC kickoff time -> the display timezone."""
        if dt is None:
            return None
        return dt.astimezone(self.tz)

    def now_local(self):
        return datetime.now(self.tz)

    def logo(self, team, size=20, flip=False, league=None):
        """Fetch a logo, or None when the abbreviation would read better.

        Bundled helmet art is NFL-only; see data/logos.py.
        """
        style = str(self.ctx.config.get("display.logo_style", "auto")).lower()
        forced_text = {t.upper() for t in
                       (self.ctx.config.get("display.logo_text_teams") or [])}
        if style == "text" or team.abbr.upper() in forced_text:
            return None

        key = league or (getattr(self, "game", None) and self.game.league) or ""
        nfl = key == "nfl"
        return logos.get(team.abbr, team.logo_url, size=size,
                         helmet=self.ctx.helmet_logos and nfl, flip=flip,
                         league=key, require_legible=style != "logo")


def scroll_x(elapsed, content_width, view_width, speed=14.0, pause=1.2):
    """Left-scrolling offset with a pause at each end.

    Returns 0 when the content already fits, so callers can use this
    unconditionally.
    """
    if content_width <= view_width:
        return 0
    travel = content_width - view_width
    duration = travel / max(speed, 1.0)
    cycle = duration + pause * 2
    t = elapsed % cycle
    if t < pause:
        return 0
    if t < pause + duration:
        return int((t - pause) * speed)
    return int(travel)


def blink(elapsed, period=1.0, duty=0.5):
    return (elapsed % period) < period * duty


class GameScreen(Screen):
    """Shared chrome for the three game states.

    All three put 20px logos in the top corners and keep a 23px centre column
    between them, so a game looks like the same object whether it is about to
    start, in progress, or over.
    """

    is_info = False
    LOGO = 20
    LEFT_X = 0
    RIGHT_X = 44
    CENTER_X0 = 21
    CENTER_X1 = 43

    def __init__(self, ctx, game):
        super().__init__(ctx)
        self.game = game
        self.key = "game:{}".format(game.id)

    def draw_logos(self, frame, y=0):
        game = self.game
        away = self.logo(game.away, self.LOGO)
        # Helmet art faces right, so the home helmet is mirrored to face the
        # away team. ESPN roundels are symmetric and must not be flipped.
        flip_home = self.ctx.helmet_logos and game.league == "nfl"
        home = self.logo(game.home, self.LOGO, flip=flip_home)


        frame.logo_or_abbr(away, (self.LEFT_X, y), game.away.short, self.LOGO,
                           layout.hex_color(game.away.color))
        frame.logo_or_abbr(home, (self.RIGHT_X, y), game.home.short, self.LOGO,
                           layout.hex_color(game.home.color))
        self.draw_rank_badges(frame, y)

    def draw_rank_badges(self, frame, y=0):
        """AP rank in the outer corner of each logo.

        Drawn on a black pad because a bare yellow numeral on top of a bright
        logo is unreadable at this size.
        """
        for team, x in ((self.game.away, self.LEFT_X),
                        (self.game.home, self.RIGHT_X)):
            if not team.rank:
                continue
            label = str(team.rank)
            width = layout.text_width(frame.draw, label, FONTS.tiny)
            bx = x if x == self.LEFT_X else x + self.LOGO - width - 1
            frame.rect([bx - 1, y, bx + width, y + 6], fill=layout.BLACK)
            frame.text((bx, y + 1), label, FONTS.tiny, layout.RANK)

    def draw_league_tag(self, frame, y=26):
        """Only label the league when more than one is in the rotation."""
        if len(self.ctx.config.enabled_leagues()) > 1:
            frame.text((1, y), self.game.league_label, FONTS.tiny, layout.DARK)

    def center_text(self, frame, y, string, fnt=None, fill=layout.WHITE):
        return frame.text_centered(y, string, fnt or FONTS.small, fill,
                                   x0=self.CENTER_X0, x1=self.CENTER_X1 + 1)

    def local_start(self):
        return self.to_local(self.game.start)

    def time_string(self):
        local = self.local_start()
        if not local:
            return "TBD"
        if self.ctx.config.get("display.time_format", "12h") == "24h":
            return local.strftime("%H:%M")
        return local.strftime("%-I:%M").lstrip("0") + local.strftime("%p").lower()[:1]

    def day_string(self):
        local = self.local_start()
        if not local:
            return ""
        # Both sides must be in the display timezone. Comparing a kickoff in
        # Eastern against "today" in the Pi's system timezone is what makes a
        # Saturday noon game label itself TMRW on a UTC-configured Pi.
        delta = (local.date() - self.now_local().date()).days
        if delta == 0:
            return "TODAY"
        if delta == 1:
            return "TMRW"
        return local.strftime("%a").upper()
