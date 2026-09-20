"""Which games belong to today, and which league the day belongs to.

A board showing two sports has a problem no single-sport board has: on Sunday
the NFL is playing and Saturday's college results are a day old, but both come
back from ESPN in the same shape and the rotation treats them identically. The
panel spends half its time on finished games from yesterday while live games
are going on.

The obvious fix is a weekday table -- Saturday is college, Sunday is the NFL --
and it is wrong. Thanksgiving is NFL on a Thursday. Black Friday has both. The
college season ends in January with playoff games on weeknights, and bowl
season puts games on every day between Christmas and New Year. A table needs an
exception for each of those, and the exceptions are what break.

So the day decides itself, from the games. A game is part of today's slate if
it is live now or it belongs to today's date locally -- which includes the ones
that have not kicked off yet, so an NFL Sunday is an NFL Sunday from midnight,
not from one o'clock. A league with any game in today's slate is active. On
Sunday that is the NFL; college has nothing today and goes quiet on its own.

Everything here is a pure function over a list of games and a timezone, so the
rules can be tested against a made-up calendar rather than waiting for a real
Sunday to come round.
"""

from datetime import datetime, timezone


def local_date(game, tz=None):
    """The calendar date a game belongs to, in the viewer's timezone.

    Kickoff times arrive as UTC, and the difference matters: a 8:15pm Eastern
    Sunday night game is 00:15 Monday UTC, so using the UTC date would file it
    under the wrong day and make the NFL look stale on Sunday night, which is
    the one moment it certainly is not.
    """
    if getattr(game, "start", None) is None:
        return None
    try:
        return game.start.astimezone(tz).date()
    except Exception:
        return None


def slate_day(games, tz=None, now=None):
    """The date the board should treat as the current slate.

    Normally today. When today has nothing -- a Wednesday in October, all of
    June -- falling through to an empty panel would be worse than showing
    something, so the nearest day that does have games is used instead, with
    ties going to the future: on a Wednesday, Sunday's upcoming slate beats
    last Sunday's results.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(tz).date()
    if not games:
        return today

    if any(getattr(g, "live", False) or local_date(g, tz) == today
           for g in games):
        return today

    dates = [d for d in (local_date(g, tz) for g in games) if d is not None]
    if not dates:
        return today
    return min(dates, key=lambda d: (abs((d - today).days), 0 if d >= today else 1))


def active_leagues(games, tz=None, now=None):
    """League keys with a game in the current slate.

    A live game always counts, whatever date it started on. A college game
    still in the fourth quarter at 12:30am counts as current, because it is:
    the clock is running.
    """
    day = slate_day(games, tz, now)
    active = set()
    for game in games:
        if getattr(game, "live", False) or local_date(game, tz) == day:
            active.add(getattr(game, "league", ""))
    return active


def describe(games, tz=None, now=None):
    """One line for the status output and the web UI.

    Worth having because "why is my board not showing college" is otherwise a
    question with no visible answer.
    """
    day = slate_day(games, tz, now)
    active = sorted(a for a in active_leagues(games, tz, now) if a)
    if not active:
        return "no current slate"
    return "{}: {}".format(day.isoformat(), ", ".join(active))
