"""Builds the ordered list of screens the panel walks through.

The rules that matter, in priority order:

  1. A favourite team playing live owns the panel, unless it is halftime.
  2. Only leagues with games today are in the rotation at all. On Sunday that
     is the NFL, and Saturday's finished college games drop out instead of
     competing with live football. Your own teams still come round on an off
     day, just not every pass.
  3. What survives that is filtered (favourites, ranked matchups, live games)
     and capped, so a 40-game Saturday still comes back around to your team
     inside a couple of minutes.
  4. Info screens are interleaved every N games rather than bolted on the end,
     so the poll and the standings actually get seen -- and a league that is
     not playing today does not contribute a poll or standings either.

The playlist is rebuilt whenever the data generation changes, but the rotation
index is carried across rebuilds by screen key. Without that, a score update
every 20 seconds would snap the panel back to the first game forever.
"""

from data import hardware, leagues, slate
from renderer.screens import fantasy as fantasy_screens
from renderer.screens import game as game_screens
from renderer.screens import info as info_screens


class Playlist:
    def __init__(self, ctx):
        self.ctx = ctx
        self.screens = []
        # Screens held in the leftmost cells rather than rotating. Only ever
        # used on a tiled panel: a single panel that camps on a game simply has
        # that one game as its whole playlist.
        self.pinned = []
        self.index = 0
        self._generation = -1
        self._config_version = -1
        # Info screens already shown this pass. The playlist is rebuilt on
        # every data refresh (every 20s while games are live), so a cycle
        # created inside the build would restart at the first item each time
        # and the clock, weather and standings would never be reached. This
        # set survives rebuilds and is what guarantees full coverage.
        self._shown_info = set()
        # Completed rotations. Used only to bring an off-day favourite back
        # every few passes rather than every one: a Saturday result on Sunday
        # is worth seeing, and worth seeing much less often than the game
        # currently being played.
        self._passes = 0
        # Leagues with a game in today's slate, recomputed each build. Held on
        # the instance so the info screens can honour the same decision as the
        # game list without computing it twice.
        self._active = None

    # ---------------------------------------------------------------- build

    def maybe_rebuild(self, force=False):
        store, config = self.ctx.store, self.ctx.config
        if (not force
                and store.generation == self._generation
                and config.version == self._config_version):
            return False

        current_key = self.current().key if self.screens else None
        self.pinned = []
        self.screens = self._build()
        self._generation = store.generation
        self._config_version = config.version

        # Keep showing whatever was on screen if it still exists.
        for i, screen in enumerate(self.screens):
            if screen.key == current_key:
                self.index = i
                break
        else:
            self.index = min(self.index, max(0, len(self.screens) - 1))
        return True

    def _eligible_games(self):
        store, config = self.ctx.store, self.ctx.config
        games = store.snapshot()
        if not games:
            return []

        favourites_only = config.get("rotation.favorites_only", False)
        keep = []
        for game in games:
            if game.is_favorite:
                keep.append(game)
                continue
            if favourites_only:
                continue
            league = leagues.get(game.league)
            # "Top 25 only" means at least one ranked team, which is the
            # sensible reading: an unranked team beating a ranked one is
            # exactly the game you want on the board.
            if league and league.ranked and config.ranked_only(game.league):
                if game.best_rank > 25:
                    continue
            keep.append(game)

        keep = self._current_slate(keep)

        # Favourites are never dropped by the cap. Slicing the list plainly
        # meant that on a 91-game Saturday your own team could fall off the end
        # of the rotation, which is the one game the board exists to show.
        favourites = [g for g in keep if g.is_favorite]
        others = [g for g in keep if not g.is_favorite]

        cap = int(config.get("rotation.max_games", 14) or 0)
        if cap > 0:
            room = max(0, cap - len(favourites))
            others = self._share_cap_across_leagues(others, room)

        return self._ordered_by_league(favourites + others)

    def _current_slate(self, games):
        """Drop leagues that are not playing today, keeping your teams.

        This is the rule that stops a Sunday rotation being half Saturday.
        College results a day old stop competing with live NFL games for the
        panel; they are out of the loop until college plays again.

        Favourites are the deliberate exception, because "did OSU win" is still
        a question on Sunday morning. They come back on one pass in three, so
        the result stays available without the board spending a third of its
        time on yesterday.
        """
        config = self.ctx.config
        self._active = None
        if not games or not config.get("rotation.day_relevance", True):
            return games

        active = slate.active_leagues(games, self._tz())
        if not active:
            return games

        self._active = active
        current = [g for g in games if g.league in active]
        if not current:
            # Every league stale at once must not blank the panel. The slate
            # helper already falls back to the nearest day that has games, so
            # this only fires on data odd enough that no league matched at all.
            self._active = None
            return games

        stale_favourites = [g for g in games
                            if g.league not in active and g.is_favorite]
        if stale_favourites and self._offday_turn():
            current += stale_favourites
        return current

    def _offday_turn(self):
        """Is this one of the passes an off-day favourite appears on?"""
        every = int(self.ctx.config.get("rotation.offday_favorite_every", 3) or 1)
        if every <= 1:
            return True
        return self._passes % every == 0

    def _tz(self):
        """The display timezone, or None to mean system local time."""
        from renderer.screens.base import resolve_tz
        return resolve_tz(self.ctx.config.get("display.timezone", ""))

    def _plays_today(self, league_key):
        """Whether a league contributes info screens this pass.

        The poll and the standings are league-scoped, so on an NFL Sunday the
        AP Top 25 pages are three screens about a competition that is not
        playing. Same decision as the game list, applied to the same leagues.
        """
        return self._active is None or league_key in self._active

    def _league_order(self):
        """League keys in the order the rotation should walk them."""
        config = self.ctx.config
        order = [str(k).lower() for k in
                 (config.get("rotation.league_order") or [])]
        for key in config.enabled_leagues():
            if key not in order:
                order.append(key)
        return order

    def _ordered_by_league(self, games):
        """All of one league, then all of the next.

        Interleaving by kickoff time reads as a jumble on a board showing two
        sports at once: college and NFL scores look alike, and without a
        grouping you cannot tell which slate you are looking at. Grouping makes
        the rotation legible -- college comes round, then the NFL.
        """
        if not self.ctx.config.get("rotation.group_by_league", True):
            return games
        order = self._league_order()

        def rank(game):
            try:
                return order.index(game.league)
            except ValueError:
                return len(order)

        return sorted(games, key=rank)      # stable: keeps kickoff order within

    def _share_cap_across_leagues(self, games, room):
        """Fill the cap by taking turns between leagues.

        Necessary because of the grouping above: sort college first and then
        take the first 14 of a 91-game Saturday and the NFL never appears at
        all. Taking turns means both slates are represented, and the grouping
        then decides what order they are shown in.
        """
        if room <= 0:
            return []
        buckets = {}
        for game in games:
            buckets.setdefault(game.league, []).append(game)

        order = [k for k in self._league_order() if k in buckets]
        order += [k for k in buckets if k not in order]

        out = []
        index = 0
        while len(out) < room and any(buckets[k] for k in order):
            key = order[index % len(order)]
            if buckets[key]:
                out.append(buckets[key].pop(0))
            index += 1
        return out

    def _info_pool(self, games):
        """Every info screen currently worth showing, in a stable order."""
        config, store = self.ctx.config, self.ctx.store
        pool = []

        if config.get("screens.rankings"):
            for key in config.enabled_leagues():
                league = leagues.get(key)
                if not league or not league.rankings_path:
                    continue
                if not self._plays_today(key):
                    continue
                if store.get_rankings(key):
                    for page in range(3):        # top 9 is plenty for a loop
                        pool.append(info_screens.RankingsScreen(self.ctx, key, page))

        if config.get("screens.team_summary"):
            for key in config.enabled_leagues():
                # A favourite's stats card follows the favourite itself: on the
                # league's off day it appears on that same one pass in three,
                # not on every one.
                if not self._plays_today(key) and not self._offday_turn():
                    continue
                for abbr in config.favorites(key):
                    card = info_screens.TeamCardScreen(self.ctx, key, abbr)
                    # A team on a bye has nothing to put on the card. Leaving it
                    # in the rotation spends four seconds telling you your team
                    # is not playing, which you already know.
                    if card.has_game():
                        pool.append(card)

        if config.get("screens.leaders"):
            for game in games:
                if game.leaders and (game.live or game.final):
                    pool.append(info_screens.LeadersScreen(self.ctx, game))

        if config.get("screens.standings"):
            for key in config.enabled_leagues():
                if not self._plays_today(key):
                    continue
                entries = store.get_standings(key)
                if not entries:
                    continue
                divisions = []
                for entry in entries:
                    if entry.division and entry.division not in divisions:
                        divisions.append(entry.division)
                favourites = set(config.favorites(key))
                # Lead with the divisions your teams are actually in.
                divisions.sort(key=lambda d: 0 if any(
                    e.division == d and e.abbr in favourites for e in entries) else 1)
                for division in divisions[:4]:
                    pool.append(info_screens.StandingsScreen(self.ctx, key, division))

        if config.get("fantasy.enabled"):
            if store.fantasy_needs_auth:
                # A silent disappearance would go unnoticed for weeks, so the
                # expired login gets its own screen in the rotation.
                pool.append(fantasy_screens.FantasyAuthScreen(
                    self.ctx, self.ctx.config.get("identity.hostname", "")))
            else:
                state = store.get_fantasy()
                if state:
                    if config.get("fantasy.show_matchup", True) and state.my_matchup:
                        pool.append(fantasy_screens.FantasyMatchupScreen(
                            self.ctx, state.my_matchup))
                    if config.get("fantasy.show_standings", True) and state.teams:
                        pages = min(2, (len(state.teams) + 3) // 4)
                        for page in range(pages):
                            pool.append(fantasy_screens.FantasyLeagueScreen(
                                self.ctx, state, page))

        if config.get("screens.ticker") and len(games) > 1:
            pool.append(info_screens.TickerScreen(self.ctx, games))

        if config.get("screens.clock"):
            pool.append(info_screens.ClockScreen(self.ctx))

        if config.get("screens.weather") and store.get_weather():
            pool.append(info_screens.WeatherScreen(self.ctx))

        return pool

    def _build(self):
        config, store = self.ctx.config, self.ctx.store
        games = self._eligible_games()

        # ---- rule 1: camp on a live favourite ----
        #
        # On a single panel, camping means the favourite IS the playlist.
        #
        # On a chained pair that reading is wrong, and it showed: the one-screen
        # playlist got handed to both cells, so a two-panel board spent the
        # whole game displaying it twice. The point of the second panel is to
        # see something else. So the favourite is pinned to the left cell and
        # the rest of the slate keeps rotating through the others -- your game
        # always in the same place, everything else moving beside it.
        cells = max(1, int(getattr(self.ctx, "cell_count", 1) or 1))
        camping = config.get("rotation.stay_on_live_favorite", True)
        pinned_games = []

        if camping:
            for game in store.live_favorites():
                if config.get("rotation.leave_during_halftime", True) and game.halftime:
                    continue
                pinned_games.append(game)
                # Always leave one cell rotating. Filling every cell with
                # pinned games is the same redundancy in a different shape.
                if len(pinned_games) >= max(1, cells - 1):
                    break

            if pinned_games and cells == 1:
                return [game_screens.for_game(self.ctx, pinned_games[0])]

            if pinned_games:
                self.pinned = [game_screens.for_game(self.ctx, g)
                               for g in pinned_games]

        if not config.get("rotation.enabled", True):
            if games:
                return [game_screens.for_game(self.ctx, games[0])]

        pinned_ids = {g.id for g in pinned_games}
        games = [g for g in games if g.id not in pinned_ids]

        game_list = [game_screens.for_game(self.ctx, g) for g in games]
        info_pool = self._info_pool(games)

        if not game_list:
            # Offseason or an empty slate: run the info screens on their own
            # rather than showing a dead panel.
            if info_pool:
                return info_pool
            offline = not store.online
            return [info_screens.MessageScreen(
                self.ctx,
                "NO GAMES" if not offline else "OFFLINE",
                "CHECK BACK LATER" if not offline else "RETRYING",
                key="idle")]

        every = max(1, int(config.get("screens.info_every", 4)))
        queue = self._info_queue(info_pool)

        out = []
        for i, screen in enumerate(game_list):
            out.append(screen)
            if queue and (i + 1) % every == 0 and (i + 1) < len(game_list):
                out.append(queue.pop(0))

        # Always close the loop with one info screen so the poll shows up even
        # on a slate shorter than `info_every`.
        if queue and len(game_list) < every:
            out.append(queue.pop(0))
        return out

    def _info_queue(self, pool):
        """Info screens in 'least recently shown first' order.

        Screens already seen this pass go to the back, so a rebuild resumes
        the rotation instead of restarting it. When everything has been seen
        the pass resets and the cycle begins again.
        """
        if not pool:
            return []
        unseen = [s for s in pool if s.key not in self._shown_info]
        if not unseen:
            self._shown_info.clear()
            unseen = list(pool)
        seen = [s for s in pool if s.key in self._shown_info]
        return unseen + seen

    # ---------------------------------------------------------------- walk

    def current(self):
        if not self.screens:
            return info_screens.MessageScreen(self.ctx, "LOADING", key="boot")
        return self.screens[self.index % len(self.screens)]

    def window(self, count):
        """The screens to draw across `count` cells, left to right.

        Pinned screens take the leftmost cells and stay put; the rest of the
        window rotates. When the rotating list is shorter than the cells left
        over it repeats rather than leaving one black, which looks broken; a
        duplicated game reads as intentional.
        """
        pinned = self.pinned[:max(0, count - 1)] if count > 1 else []
        rest = count - len(pinned)
        if not self.screens:
            filler = self.current()
            return pinned + [filler] * rest
        total = len(self.screens)
        rotating = [self.screens[(self.index + i) % total] for i in range(rest)]
        return pinned + rotating

    def advance(self):
        if not self.screens:
            return self.current()
        # On a tiled panel every visible screen is being retired, not just the
        # first, so all of them count as shown.
        cells = max(1, getattr(self.ctx, "cell_count", 1))
        for screen in self.window(cells):
            if screen.is_info:
                self._shown_info.add(screen.key)

        # Step by the number of cells that actually rotate. Stepping by the
        # full cell count would skip a screen for every pinned one, so on a
        # two-panel board with a favourite pinned, half the slate would never
        # be shown.
        moving = cells - len(self.pinned[:max(0, cells - 1)]) if cells > 1 else cells
        before = self.index
        self.index = (self.index + max(1, moving)) % len(self.screens)
        # A wrap is one completed pass over the rotation, which is the unit the
        # off-day favourite throttle counts in. Counted here rather than per
        # rebuild, because the playlist is rebuilt every 20 seconds while games
        # are live and that has nothing to do with how much of it you have seen.
        if self.index <= before:
            self._passes += 1
        return self.current()

    def __len__(self):
        return len(self.screens)
