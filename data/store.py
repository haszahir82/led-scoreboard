"""Background data store.

The single most important structural change from upstream: the render loop
never makes an HTTP request. A daemon thread refreshes everything on its own
cadence and the renderer reads whatever snapshot is current. That is why the
panel keeps animating smoothly while ESPN is slow, and why a network outage
shows stale scores instead of a frozen or crashed display.

Refresh cadence adapts: ~20s while anything tracked is live, ~5 minutes when
the slate is all pregame or final.
"""

import threading
import time
from datetime import datetime, timezone

from . import espn, fantasy, geocode, hardware, leagues, logos
from . import weather as weather_mod


class DataStore:
    def __init__(self, config):
        self.config = config
        self._lock = threading.RLock()

        self.games = []              # list[Game], all enabled leagues
        self.rankings = {}           # league_key -> list[RankingEntry]
        self.standings = {}          # league_key -> list[StandingEntry]
        self.weather = None
        self.location = None
        self.fantasy = None          # LeagueState, or None
        self.fantasy_error = ""      # human-readable, shown in the web UI
        self.fantasy_needs_auth = False

        self.last_success = 0.0
        self.last_attempt = 0.0
        self.online = True
        self.error = ""
        self.generation = 0          # bumped whenever games change

        self._stop = threading.Event()
        self._thread = None
        self._wake = threading.Event()
        self._slow_last = 0.0
        self._fantasy_last = 0.0

    # ---------- lifecycle ----------

    def start(self):
        self.refresh_games(blocking=True)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="data-refresh")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self):
        """Force an immediate refresh, e.g. after a config change."""
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            interval = self._interval()
            self._wake.wait(timeout=interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.refresh_games()
                self._refresh_fantasy()
                self._refresh_slow()
            except Exception as exc:
                self.error = str(exc)

    def _interval(self):
        """How long to wait before the next refresh.

        Both numbers come from the detected board unless pinned in config: an
        HTTPS round trip costs real CPU on a single ARMv6 core, and that CPU
        is the same one driving the panel.
        """
        key = "live_seconds" if self.any_live() else "idle_seconds"
        return float(hardware.tuned(self.config, key) or (20 if "live" in key else 300))

    # ---------- refresh ----------

    def refresh_games(self, blocking=False):
        collected, errors = [], []
        for key in self.config.enabled_leagues():
            if not leagues.get(key):
                continue
            try:
                collected.extend(espn.scoreboard_window(key))
            except Exception as exc:
                errors.append("{}: {}".format(key, exc))

        self.last_attempt = time.time()

        # A total failure keeps the previous snapshot on screen. Showing a
        # score that is two minutes stale beats showing nothing at all.
        if errors and not collected:
            self.online = False
            self.error = "; ".join(errors)[:200]
            return

        self._tag(collected)
        collected.sort(key=self._sort_key)

        with self._lock:
            self.games = collected
            self.generation += 1
            self.online = True
            self.error = "; ".join(errors)[:200] if errors else ""
            self.last_success = time.time()

        # Both sizes the screens actually ask for. Warming only 20 meant the
        # team card, which draws at 16, was a guaranteed cache miss the first
        # time it appeared -- and a miss falls back to the abbreviation, so a
        # favourite team showed four letters where its logo should be.
        sizes = (20, 16)
        helmet = bool(self.config.get("display.use_helmet_logos", True))

        def warm(games):
            for size in sizes:
                logos.prefetch(games, size=size, helmet=helmet)

        if blocking:
            # Only the first screens need logos before the first frame; the
            # rest are fetched on the background thread.
            count = int(hardware.tuned(self.config, "logo_prefetch") or 12)
            warm(collected[:count])
        else:
            threading.Thread(target=warm, args=(collected,),
                             daemon=True).start()

    def _refresh_slow(self):
        """Rankings, standings and weather change slowly; poll them rarely."""
        now = time.time()
        if now - self._slow_last < float(
                self.config.get("refresh.rankings_seconds", 3600)):
            return
        self._slow_last = now

        for key in self.config.enabled_leagues():
            league = leagues.get(key)
            if not league:
                continue
            if league.rankings_path and self.config.get("screens.rankings"):
                try:
                    poll = self.config.get("leagues.{}.poll".format(key), "ap")
                    entries = espn.rankings(key, poll=poll)
                    if entries:
                        with self._lock:
                            self.rankings[key] = entries
                except Exception:
                    pass
            if league.standings_path and self.config.get("screens.standings"):
                try:
                    entries = espn.standings(key)
                    if entries:
                        with self._lock:
                            self.standings[key] = entries
                except Exception:
                    pass

        if self.config.get("weather.enabled") and self.config.get("screens.weather"):
            try:
                # Resolves the configured ZIP to coordinates the first time,
                # then reads them from cache on every subsequent refresh.
                location = geocode.resolve_config(self.config)
                if location:
                    current = weather_mod.fetch(
                        location.latitude, location.longitude,
                        self.config.get("weather.units", "imperial"))
                    with self._lock:
                        self.weather = current
                        self.location = location
            except Exception:
                pass

    def _refresh_fantasy(self):
        """Fantasy scores move with NFL plays, so this has its own cadence."""
        config = self.config
        if not config.get("fantasy.enabled"):
            return
        league_id = config.get("fantasy.league_id") or ""
        if not league_id:
            return

        interval = float(config.get("fantasy.refresh_seconds", 120) or 120)
        if time.time() - self._fantasy_last < interval:
            return
        self._fantasy_last = time.time()

        try:
            state = fantasy.league_state(
                league_id,
                game=config.get("fantasy.game", "football"),
                my_team_id=config.get("fantasy.team_id"))
        except fantasy.AuthRequired as exc:
            with self._lock:
                self.fantasy_needs_auth = True
                self.fantasy_error = str(exc)
            return
        except Exception as exc:
            # Keep the last good standings on screen rather than blanking.
            with self._lock:
                self.fantasy_error = str(exc)
            return

        with self._lock:
            self.fantasy = state
            self.fantasy_error = ""
            self.fantasy_needs_auth = False

    def get_fantasy(self):
        with self._lock:
            return self.fantasy

    # ---------- tagging and ordering ----------

    def _tag(self, games):
        favorites = self.config.all_favorites()
        for game in games:
            favs = favorites.get(game.league, [])
            game.is_favorite = game.involves(favs) if favs else False

    def _sort_key(self, game):
        """Order the slate: favorites first, then live, then by rank/time.

        This ordering is what the rotation walks, so it is also what decides
        which games survive the max_games cap.
        """
        state_rank = {"in": 0, "pre": 1, "post": 2}.get(game.state, 3)
        start = game.start or datetime.now(timezone.utc)
        return (
            0 if game.is_favorite else 1,
            state_rank,
            0 if game.is_ranked_matchup else 1,
            game.best_rank,
            start,
        )

    # ---------- reads ----------

    def snapshot(self):
        with self._lock:
            return list(self.games)

    def any_live(self):
        with self._lock:
            return any(g.live for g in self.games)

    def favorite_games(self):
        with self._lock:
            return [g for g in self.games if g.is_favorite]

    def live_favorite(self):
        for game in self.favorite_games():
            if game.live:
                return game
        return None

    def live_favorites(self):
        """Every favourite currently playing, in favourite order.

        A single panel can only camp on one of these, but a chained pair can
        hold one and keep rotating in the other, so the caller needs the list
        rather than the first hit.
        """
        return [game for game in self.favorite_games() if game.live]

    def get_rankings(self, league_key):
        with self._lock:
            return list(self.rankings.get(league_key, []))

    def get_standings(self, league_key):
        with self._lock:
            return list(self.standings.get(league_key, []))

    def get_weather(self):
        with self._lock:
            return self.weather

    def stale_seconds(self):
        return time.time() - self.last_success if self.last_success else 0
