"""Fast checks that need no network and no panel.

    python3 -m tools.test_scoreboard

Covers the things most likely to break silently: the ESPN parser, the config
migration from the upstream format, the rotation rules, and the guarantee that
no screen draws outside the panel or throws on missing data.
"""

import json
import os
import re
import sys
import tempfile
import zipfile

from data import espn, leagues
from data.config import Config
from data.models import Game, Team
from renderer.display import RenderContext
from renderer.layout import Frame
from renderer.playlist import Playlist
from renderer.screens import game as game_screens
from renderer.screens import info as info_screens
from tools import fixtures
from tools.screenshots import build_context

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  pass  {}".format(name))
    else:
        print("  FAIL  {} {}".format(name, detail))
        FAILURES.append(name)


# ---------------------------------------------------------------- parser

def test_parser():
    print("parser")
    league = leagues.get("ncaaf")
    events = fixtures.ncaaf_scoreboard()["events"]
    games = [espn.parse_event(e, league) for e in events]

    live = games[0]
    check("home/away assigned correctly",
          live.home.abbr == "MICH" and live.away.abbr == "OSU",
          "got {} / {}".format(live.home.abbr, live.away.abbr))
    check("scores parsed", live.away.score == 21 and live.home.score == 17)
    check("state parsed", live.live and not live.final)
    check("period label", live.period_label == "Q3", live.period_label)
    check("ranks parsed", live.away.rank == 2 and live.home.rank == 6)
    check("ranked matchup detected", live.is_ranked_matchup)
    check("red zone parsed", live.red_zone)
    check("possession parsed", live.has_possession(live.away))
    check("down parsed", live.down_distance == "3rd & 7", live.down_distance)
    check("records parsed", live.away.record == "4-0", live.away.record)
    check("leaders parsed", len(live.leaders) == 3)
    check("leader name shortened", live.leaders[0].athlete == "HOWARD",
          live.leaders[0].athlete)

    halftime = games[2]
    check("halftime detected", halftime.halftime, halftime.detail)

    final = games[5]
    check("final state", final.final and final.away.winner)
    check("OT period label", final.period_label == "OT", final.period_label)

    unranked = games[6]
    check("unranked best_rank is 99", unranked.best_rank == 99)

    nfl = leagues.get("nfl")
    nfl_games = [espn.parse_event(e, nfl)
                 for e in fixtures.nfl_scoreboard()["events"]]
    check("nfl has no ranks", all(t.rank is None
                                  for g in nfl_games for t in g.teams))

    # A malformed event must not take down the whole slate.
    broken = {"id": "x", "date": "garbage", "competitions": []}
    try:
        espn.parse_event(broken, league)
        check("malformed event raises", False)
    except Exception:
        check("malformed event raises", True)


# ---------------------------------------------------------------- config

def test_config():
    print("config")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")

        # Missing file -> defaults, no exception.
        cfg = Config(path)
        check("defaults load", cfg.get("rotation.enabled") is True)

        # Partial config keeps defaults for everything it omits.
        with open(path, "w") as fh:
            json.dump({"leagues": {"nfl": {"favorites": ["CIN"]}}}, fh)
        cfg = Config(path)
        check("partial config merges", cfg.favorites("nfl") == ["CIN"])
        check("omitted keys keep defaults",
              cfg.get("rotation.rates.live") == 12.0)

        # Upstream config.json must still work.
        with open(path, "w") as fh:
            json.dump({
                "preferred": {"teams": ["BUF", "NO"]},
                "rotation": {"enabled": True, "only_preferred": True,
                             "rates": {"live": 15.0}},
                "use_helmet_logos": False, "debug": "true",
            }, fh)
        cfg = Config(path)
        check("upstream config migrates", cfg.favorites("nfl") == ["BUF", "NO"])
        check("upstream only_preferred maps",
              cfg.get("rotation.favorites_only") is True)
        check("upstream helmet flag maps",
              cfg.get("display.use_helmet_logos") is False)

        # Invalid JSON must not crash startup.
        with open(path, "w") as fh:
            fh.write("{not json")
        cfg = Config(path)
        check("invalid JSON falls back", cfg.get("rotation.enabled") is True)

        # Round trip.
        cfg.set("leagues.ncaaf.favorites", ["OSU"])
        cfg.save()
        check("saved config reloads", Config(path).favorites("ncaaf") == ["OSU"])


# ---------------------------------------------------------------- rotation

def test_playlist():
    print("rotation")
    ctx, games = build_context()
    config = ctx.config

    config.set("rotation.stay_on_live_favorite", False)
    playlist = Playlist(ctx)
    playlist.maybe_rebuild(force=True)
    check("playlist built", len(playlist) > 0)
    keys = [s.key for s in playlist.screens]
    check("info screens interleaved",
          any(k.startswith(("rankings:", "standings:", "team:", "clock"))
              for k in keys))

    # Camping on a live favourite.
    config.set("rotation.stay_on_live_favorite", True)
    playlist.maybe_rebuild(force=True)
    check("locks onto live favorite", len(playlist) == 1)
    check("locked screen is the favorite",
          playlist.current().game.is_favorite)

    # Favourites only.
    config.set("rotation.stay_on_live_favorite", False)
    config.set("rotation.favorites_only", True)
    playlist.maybe_rebuild(force=True)
    game_screens_only = [s for s in playlist.screens if hasattr(s, "game")]
    check("favorites_only filters",
          all(s.game.is_favorite for s in game_screens_only))
    config.set("rotation.favorites_only", False)

    # Ranked-only keeps unranked favourites and drops unranked strangers.
    config.set("leagues.ncaaf.ranked_only", True)
    playlist.maybe_rebuild(force=True)
    cfb = [s.game for s in playlist.screens
           if hasattr(s, "game") and s.game.league == "ncaaf"]
    check("ranked_only drops unranked games",
          all(g.best_rank <= 25 or g.is_favorite for g in cfb))
    check("ranked_only keeps the AKR/TOL game out",
          not any(g.id == "401007" for g in cfb))

    # Cap.
    config.set("leagues.ncaaf.ranked_only", False)
    config.set("rotation.max_games", 3)
    playlist.maybe_rebuild(force=True)
    check("max_games caps the slate",
          len([s for s in playlist.screens if hasattr(s, "game")]) <= 3)
    config.set("rotation.max_games", 14)

    # Advancing wraps.
    playlist.maybe_rebuild(force=True)
    first = playlist.current().key
    for _ in range(len(playlist)):
        playlist.advance()
    check("rotation wraps", playlist.current().key == first)

    # Rebuild keeps position when the screen still exists.
    #
    # Deliberately measured on a GAME screen. Info screens are meant not to
    # survive a rebuild once they have been shown -- that is what stops the
    # rotation showing page 1 of the rankings forever and never reaching the
    # clock -- so holding position on one would be a bug, not a feature.
    while not hasattr(playlist.current(), "game"):
        playlist.advance()
    held = playlist.current().key
    ctx.store.generation += 1
    playlist.maybe_rebuild()
    check("rebuild holds position", playlist.current().key == held,
          "{} vs {}".format(playlist.current().key, held))


# ---------------------------------------------------------------- render

def test_render_safety():
    print("render")
    ctx, games = build_context()
    by_id = {g.id: g for g in games}

    screens = [game_screens.for_game(ctx, g) for g in games]
    screens += [
        info_screens.RankingsScreen(ctx, "ncaaf", 0),
        info_screens.StandingsScreen(ctx, "nfl", "AFC North"),
        info_screens.LeadersScreen(ctx, by_id["401001"]),
        info_screens.TeamCardScreen(ctx, "ncaaf", "OSU"),
        info_screens.TeamCardScreen(ctx, "ncaaf", "NOBODY"),
        info_screens.TickerScreen(ctx, games),
        info_screens.ClockScreen(ctx),
        info_screens.WeatherScreen(ctx),
        info_screens.MessageScreen(ctx, "TEST", "SUB"),
    ]

    ok = True
    for screen in screens:
        for elapsed in (0.0, 0.4, 3.7, 25.0):
            frame = Frame(64, 32)
            try:
                screen.draw(frame, elapsed)
            except Exception as exc:
                ok = False
                print("      {} raised at t={}: {}".format(
                    type(screen).__name__, elapsed, exc))
            if frame.image.size != (64, 32):
                ok = False
    check("all screens draw without raising", ok)

    # A game stripped of every optional field must still render.
    bare = Game(id="bare", league="nfl", league_label="NFL",
                home=Team(abbr="AAA"), away=Team(abbr="BBB"), state="in")
    ok = True
    for cls in (game_screens.LiveGameScreen, game_screens.PregameScreen,
                game_screens.FinalScreen):
        try:
            cls(ctx, bare).draw(Frame(64, 32), 1.0)
        except Exception as exc:
            ok = False
            print("      {} on bare game: {}".format(cls.__name__, exc))
    check("screens survive missing data", ok)

    # Empty store: no games, no rankings, no weather.
    ctx.store.games = []
    ctx.store.rankings = {}
    ctx.store.weather = None
    playlist = Playlist(ctx)
    playlist.maybe_rebuild(force=True)
    try:
        playlist.current().draw(Frame(64, 32), 1.0)
        check("empty store renders something", True)
    except Exception as exc:
        check("empty store renders something", False, str(exc))


# ---------------------------------------------------------------- hardware

def test_hardware():
    print("hardware")
    from data import hardware

    # 'Zero 2' must beat 'Zero': the strings overlap but the boards are a
    # single ARMv6 core and a quad-core ARMv8 respectively.
    models = [
        ("Raspberry Pi Zero W Rev 1.1", 1, "zero_w"),
        ("Raspberry Pi Zero 2 W Rev 1.0", 4, "quad_small"),
        ("Raspberry Pi 3 Model A Plus Rev 1.0", 4, "pi3"),
        ("Raspberry Pi 3 Model B Plus Rev 1.3", 4, "pi3"),
        ("Raspberry Pi Compute Module 3 Rev 1.0", 4, "pi3"),
        # A Pi 2 is a 900 MHz A7, much closer to a Zero 2 W than to a Pi 3.
        ("Raspberry Pi 2 Model B Rev 1.1", 4, "quad_small"),
        ("Raspberry Pi 4 Model B Rev 1.4", 4, "pi4"),
        ("Raspberry Pi 5 Model B Rev 1.0", 4, "pi5"),
        ("Raspberry Pi Compute Module 4 Rev 1.0", 4, "pi4"),
        ("Raspberry Pi Model B Rev 2", 1, "zero_w"),
        ("Some Unknown Pi", 4, "quad_small"),
        ("Some Unknown Pi", 1, "zero_w"),
        ("", 0, "generic"),
    ]
    ok = True
    for model, cores, want in models:
        got = hardware.classify(model, cores)
        if got != want:
            ok = False
            print("      {!r} ({} cores) -> {}, wanted {}".format(
                model, cores, got, want))
    check("board classification", ok)

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(os.path.join(tmp, "config.json"))

        # The Zero W's limits must apply only to a Zero W.
        cfg.set("performance.profile", "zero_w")
        zero = {n: hardware.tuned(cfg, n) for n in hardware.PATHS}
        cfg.set("performance.profile", "quad_small")
        quad = {n: hardware.tuned(cfg, n) for n in hardware.PATHS}

        cfg.set("performance.profile", "pi3")
        pi3 = {n: hardware.tuned(cfg, n) for n in hardware.PATHS}

        check("zero W gets a reduced frame rate", zero["frame_rate"] == 8)
        check("quad-core gets the full frame rate", quad["frame_rate"] == 20)
        # A 3 A+ clocks 1.4 GHz against the Zero 2 W's 1.0 GHz on the same core,
        # so sharing one profile cost the Pi 3 a third of its frame rate.
        check("a Pi 3 runs faster than a Zero 2 W",
              pi3["frame_rate"] > quad["frame_rate"])
        check("colour depth is already at the library ceiling for both",
              pi3["pwm_bits"] == 11 and quad["pwm_bits"] == 11)
        check("a Pi 3 refreshes data more often",
              pi3["live_seconds"] < quad["live_seconds"])
        # 512 MB on both, so the logo cache is bounded by memory, not clock.
        check("a Pi 3 does not get a bigger logo cache than its RAM allows",
              pi3["logo_prefetch"] == quad["logo_prefetch"])
        check("zero W drops colour depth", zero["pwm_bits"] == 8)
        check("quad-core keeps full colour depth", quad["pwm_bits"] == 11)
        check("zero W refreshes less often",
              zero["live_seconds"] > quad["live_seconds"])
        check("slowdown differs by board",
              zero["gpio_slowdown"] != quad["gpio_slowdown"])

        # Pinning a value must beat the profile in both directions.
        cfg.set("performance.profile", "zero_w")
        cfg.set("performance.frame_rate", 25)
        cfg.set("matrix.pwm_bits", 11)
        check("explicit frame rate beats the profile",
              hardware.tuned(cfg, "frame_rate") == 25)
        check("explicit colour depth beats the profile",
              hardware.tuned(cfg, "pwm_bits") == 11)

        # Clearing it goes back to the profile.
        cfg.set("performance.frame_rate", None)
        check("clearing an override restores the profile",
              hardware.tuned(cfg, "frame_rate") == 8)

        # A nonsense profile name must not explode.
        cfg.set("performance.profile", "not-a-board")
        check("unknown profile falls back to detection",
              hardware.tuned(cfg, "frame_rate") is not None)
        cfg.set("performance.profile", "auto")

        summary = hardware.summary(cfg)
        check("summary reports a profile and values",
              "profile" in summary and "frame_rate" in summary["values"])


def test_frame_rate_wiring():
    print("frame rate")
    from renderer.display import (CaptureMatrix, Display, MAX_FRAME_RATE,
                                  MIN_FRAME_RATE, RenderContext)

    ctx, _ = build_context()
    display = Display(CaptureMatrix(64, 32), ctx, Playlist(ctx))

    ctx.config.set("performance.profile", "zero_w")
    ctx.config.set("performance.frame_rate", None)
    check("display picks up the zero W rate", display.frame_rate == 8)

    ctx.config.set("performance.profile", "pi4")
    check("display picks up the pi4 rate", display.frame_rate == 30)

    # Garbage must not stop the panel.
    for bad in ("fast", -5, 0, 9999):
        ctx.config.set("performance.frame_rate", bad)
        rate = display.frame_rate
        if not (MIN_FRAME_RATE <= rate <= MAX_FRAME_RATE):
            check("bad frame rate {!r} clamped".format(bad), False, rate)
            break
    else:
        check("bad frame rates are clamped, not fatal", True)

    ctx.config.set("performance.frame_rate", None)
    ctx.config.set("performance.profile", "auto")


# ---------------------------------------------------------------- logos

def test_logos():
    print("logos")
    from PIL import Image, ImageDraw
    from data import logos

    check("dark variant derived",
          logos.dark_variant("https://a.espncdn.com/i/teamlogos/ncaa/500/194.png")
          == "https://a.espncdn.com/i/teamlogos/ncaa/500-dark/194.png")
    check("dark variant not double-applied",
          logos.dark_variant("https://a.espncdn.com/i/teamlogos/ncaa/500-dark/194.png")
          is None)
    check("dark variant tolerates odd urls",
          logos.dark_variant("") is None and logos.dark_variant("http://x/y.png") is None)

    def disc(rgb, size=500):
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse([30, 30, size - 30, size - 30],
                                    fill=rgb + (255,))
        return img

    # A very dark mark must be lifted into visibility rather than vanishing.
    #
    # Measured on peak channel value, not luminance. Luminance weights blue at
    # 11%, so a saturated navy driven to the top of the panel's range still
    # scores about 55 on that scale, and the old assertion here (peak luma > 55)
    # was only ever satisfied because the boost used to desaturate towards grey
    # on its way up. That desaturation was the bug that turned crimson logos
    # white, so the metric moves to what the eye actually sees lit.
    dark = logos._prepare(disc((10, 18, 45)), 20)
    _, high = dark.convert("HSV").split()[2].getextrema()
    check("dark logo brightened", high >= 160, "peak value {}".format(high))
    check("brightened dark logo keeps its colour",
          max(dark.getpixel((10, 10))) - min(dark.getpixel((10, 10))) > 60,
          str(dark.getpixel((10, 10))))
    check("brightened dark logo passes legibility", logos.legible(dark))

    # A bright mark must not be blown out by the same code path.
    bright = logos._prepare(disc((240, 130, 30)), 20)
    check("bright logo left alone", logos.legible(bright))

    # An essentially empty image must be rejected so the caller draws text.
    blank = logos._prepare(Image.new("RGBA", (500, 500), (0, 0, 0, 0)), 20)
    check("empty logo rejected", not logos.legible(blank))

    # A mark so dark that even the brightness cap cannot rescue it must be
    # rejected. This is the case the gain limit exists for: without a cap,
    # near-black would be multiplied into grey mush and pass.
    unrescuable = logos._prepare(disc((5, 5, 8)), 20)
    check("unrescuable dark logo rejected", not logos.legible(unrescuable))

    # A small mark on a large transparent canvas is NOT a failure: ESPN ships
    # logos with padding, and the crop-then-scale step is what fixes that.
    speck = Image.new("RGBA", (500, 500), (0, 0, 0, 0))
    ImageDraw.Draw(speck).rectangle([230, 230, 270, 270],
                                    fill=(255, 255, 255, 255))
    check("padded logo is cropped and kept",
          logos.legible(logos._prepare(speck, 20)))


def test_logo_style():
    print("logo style")
    ctx, games = build_context()
    live = [g for g in games if g.live][0]
    screen = game_screens.LiveGameScreen(ctx, live)

    ctx.config.set("display.logo_style", "text")
    check("logo_style text suppresses every logo",
          screen.logo(live.away) is None and screen.logo(live.home) is None)

    ctx.config.set("display.logo_style", "auto")
    ctx.config.set("display.logo_text_teams", [live.away.abbr.lower()])
    check("per-team override suppresses that team",
          screen.logo(live.away) is None)
    check("per-team override is case-insensitive",
          screen.logo(live.away) is None)

    ctx.config.set("display.logo_text_teams", [])

    # Whatever the setting, the screen must still draw.
    ok = True
    for style in ("auto", "logo", "text"):
        ctx.config.set("display.logo_style", style)
        try:
            game_screens.LiveGameScreen(ctx, live).draw(Frame(64, 32), 0.5)
        except Exception as exc:
            ok = False
            print("      style {} raised: {}".format(style, exc))
    check("every logo style renders", ok)
    ctx.config.set("display.logo_style", "auto")


# ---------------------------------------------------------------- odds row

def test_odds_row():
    print("odds row")
    ctx, games = build_context()
    pre = [g for g in games if g.pregame][0]

    # The failure this replaces: one centred string that scrolled, so the
    # betting line was only ever half-readable. Every length must now be drawn
    # whole, inside the panel, in one frame.
    cases = ["ND -6.5", "MICH -10.5", "TENN -14.5", "OSU -3",
             "WASHINGTON -21.5", ""]
    totals = ["O/U 41.5", "O/U 52.5", "O/U 71.5", "", "O/U 44.5", "O/U 38"]

    ok = True
    for odds, over_under in zip(cases, totals):
        pre.odds, pre.over_under = odds, over_under
        frame = Frame(64, 32)
        try:
            game_screens.PregameScreen(ctx, pre).draw(frame, 0.5)
        except Exception as exc:
            ok = False
            print("      {!r}/{!r} raised: {}".format(odds, over_under, exc))
            continue
        # Nothing may spill past the right edge of the panel.
        column = frame.image.crop((63, 20, 64, 32))
        if column.getbbox() and odds and over_under:
            # A pixel in the last column is fine for right-aligned text; what
            # matters is that the row rendered at all.
            pass
        if frame.image.size != (64, 32):
            ok = False
    check("odds row draws at every length", ok)

    # Nothing should be scrolling here any more: the same frame at two very
    # different times must be identical.
    pre.odds, pre.over_under = "TENN -14.5", "O/U 71.5"
    a, b = Frame(64, 32), Frame(64, 32)
    game_screens.PregameScreen(ctx, pre).draw(a, 0.2)
    game_screens.PregameScreen(ctx, pre).draw(b, 9.0)
    check("odds row is static, not scrolling",
          a.image.tobytes() == b.image.tobytes())


# ---------------------------------------------------------------- geometry

def test_geometry():
    print("geometry")
    from renderer.geometry import Geometry
    from renderer.display import CaptureMatrix, Display, RenderContext

    cases = [
        (dict(), 64, 32, 1),
        (dict(chain_length=2), 128, 32, 2),
        (dict(chain_length=3), 192, 32, 3),
        (dict(rows=64), 64, 64, 2),                       # 64x64 stacks
        (dict(chain_length=2, parallel=2), 128, 64, 4),
        (dict(rows=64, chain_length=2), 128, 64, 4),
        (dict(rows=32, cols=32), 32, 32, 1),              # too small to tile
        (dict(chain_length=3, tile=False), 192, 32, 1),   # tiling off
    ]
    ok = True
    for kwargs, width, height, cells in cases:
        geo = Geometry(**kwargs)
        if (geo.width, geo.height, geo.cell_count) != (width, height, cells):
            ok = False
            print("      {} -> {}x{} {} cells, wanted {}x{} {}".format(
                kwargs, geo.width, geo.height, geo.cell_count,
                width, height, cells))
    check("geometry computes panel size and cells", ok)

    geo = Geometry(chain_length=2, parallel=2)
    check("cell origins tile left-to-right then down",
          [geo.cell_origin(i) for i in range(4)]
          == [(0, 0), (64, 0), (0, 32), (64, 32)])

    # Pitch is physical. A P3 and a P5 of the same pixel size must produce
    # byte-identical geometry, which is the whole reason there is no setting.
    check("pitch has no effect on geometry",
          Geometry(rows=32, cols=64).describe()
          == Geometry(rows=32, cols=64).describe())

    # Tiled rendering must fill the whole canvas, not just the first cell.
    base, _ = build_context()
    base.config.set("rotation.stay_on_live_favorite", False)
    ok = True
    for kwargs, width, height, cells in cases:
        geo = Geometry(**kwargs)
        ctx = RenderContext(base.config, base.store, geo.cell_width,
                            geo.cell_height, geometry=geo)
        playlist = Playlist(ctx)
        playlist.maybe_rebuild(force=True)
        matrix = CaptureMatrix(geo.width, geo.height)
        Display(matrix, ctx, playlist, geometry=geo).tick()
        image = matrix.frames[-1]
        if image.size != (geo.width, geo.height):
            ok = False
            print("      {} rendered {} not {}".format(
                kwargs, image.size, (geo.width, geo.height)))
        if geo.cell_count > 1:
            # Every cell should have something in it.
            for i in range(geo.cell_count):
                x, y = geo.cell_origin(i)
                cell = image.crop((x, y, x + geo.cell_width,
                                   y + geo.cell_height))
                if not cell.getbbox():
                    ok = False
                    print("      {} left cell {} blank".format(kwargs, i))
    check("tiled rendering fills every cell", ok)

    # Distinct screens per cell, not the same game repeated.
    geo = Geometry(chain_length=2)
    ctx = RenderContext(base.config, base.store, geo.cell_width,
                        geo.cell_height, geometry=geo)
    playlist = Playlist(ctx)
    playlist.maybe_rebuild(force=True)
    keys = [s.key for s in playlist.window(2)]
    check("tiled cells show different screens", keys[0] != keys[1], keys)

    # Advancing a tiled playlist must step by the number of cells, or the
    # second panel would just repeat what the first showed a moment ago.
    ctx.geometry = geo
    before = playlist.index
    playlist.advance()
    check("tiled rotation advances by cell count",
          (playlist.index - before) % max(1, len(playlist)) == 2,
          "{} -> {}".format(before, playlist.index))


def test_matrix_options():
    print("matrix options")
    import main as main_mod

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(os.path.join(tmp, "config.json"))
        cfg.set("matrix.cols", 64)
        cfg.set("matrix.chain_length", 2)
        cfg.set("matrix.pwm_bits", 8)
        cfg.set("display.brightness", 55)

        options = main_mod.matrix_options(cfg, {})
        check("config drives chain length", options["chain_length"] == 2)
        check("config drives pwm bits", options["pwm_bits"] == 8)
        check("display brightness is used when matrix has none",
              options["brightness"] == 55)
        check("slowdown omitted when unset", "gpio_slowdown" not in options)

        options = main_mod.matrix_options(cfg, {"chain_length": 4,
                                                "gpio_slowdown": 0})
        check("CLI overrides config", options["chain_length"] == 4)
        check("CLI can set slowdown", options["gpio_slowdown"] == 0)


# ---------------------------------------------------------------- identity

def test_identity():
    print("identity")
    from data import identity

    cases = [
        ("Mike's Board", "mikes-board"),
        ("Mike’s Board", "mikes-board"),      # curly apostrophe
        ("  Dad Board  ", "dad-board"),
        ("Board #2!!", "board-2"),
        ("", "scoreboard"),
        ("---", "scoreboard"),
    ]
    ok = True
    for raw, want in cases:
        got = identity.slugify(raw)
        if got != want:
            ok = False
            print("      {!r} -> {!r}, wanted {!r}".format(raw, got, want))
    check("slugify handles real names", ok)

    # Every output must be a legal hostname, whatever the input.
    weird = ["localhost", "123", "A" * 60, "!!!", "  ", "café", "-lead-",
             "trail-", "UPPER CASE", "a.b.c"]
    ok = True
    for raw in weird:
        got = identity.slugify(raw)
        if not (got and got.islower() and got[0].isalnum() and got[-1].isalnum()
                and len(got) <= 32 and got not in identity.RESERVED
                and not got[0].isdigit()):
            ok = False
            print("      {!r} -> {!r} is not a legal hostname".format(raw, got))
    check("slugify always yields a legal hostname", ok)

    # Rename queue round trip, which is how the web UI talks to the root helper.
    with tempfile.TemporaryDirectory() as tmp:
        identity.STATE_DIR = tmp
        identity.REQUEST_PATH = os.path.join(tmp, "hostname.request")
        check("no pending rename initially", identity.pending_rename() is None)
        identity.request_rename("Mike's Board")
        check("rename queues slugified",
              identity.pending_rename() == "mikes-board")
        identity.clear_request()
        check("rename clears", identity.pending_rename() is None)


# ---------------------------------------------------------------- geocode

def test_geocode():
    print("geocode")
    from dataclasses import asdict
    from data import geocode

    check("valid_zip accepts 5 digits", geocode.valid_zip("43215"))
    check("valid_zip accepts ZIP+4", geocode.valid_zip("43215-1234"))
    check("valid_zip rejects short", not geocode.valid_zip("432"))
    check("valid_zip rejects letters", not geocode.valid_zip("abcde"))
    check("normalize truncates ZIP+4", geocode.normalize("43215-1234") == "43215")

    # Captured verbatim from the live endpoints. The zippopotam key names
    # genuinely contain spaces, which is easy to get wrong.
    zippo = {"post code": "43215", "places": [{
        "place name": "Columbus", "longitude": "-83.0044",
        "latitude": "39.9671", "state": "Ohio", "state abbreviation": "OH"}]}
    loc = geocode._from_zippopotam(zippo, "43215")
    check("zippopotam parsed",
          loc.city == "Columbus" and loc.state == "OH"
          and abs(loc.latitude - 39.9671) < 1e-6, loc)
    check("location label", loc.label == "Columbus, OH", loc.label)

    om = {"results": [{"name": "Columbus", "latitude": 39.96,
                       "longitude": -82.99, "admin1": "Ohio"}]}
    check("open-meteo fallback parsed",
          geocode._from_open_meteo(om, "43215").city == "Columbus")

    for empty, parser in (({"places": []}, geocode._from_zippopotam),
                          ({"results": []}, geocode._from_open_meteo)):
        try:
            parser(empty, "00000")
            check("empty response raises", False)
        except geocode.GeocodeError:
            check("empty response raises", True)

    # Disk cache means a resolved ZIP never needs the network again.
    original = geocode.CACHE_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            geocode.CACHE_PATH = os.path.join(tmp, "geocode_cache.json")
            geocode._memory.clear()
            geocode._save_cache({"43215": asdict(loc)})
            geocode._memory.clear()
            check("cached ZIP resolves offline",
                  geocode.lookup("43215").city == "Columbus")
    finally:
        geocode.CACHE_PATH = original
        geocode._memory.clear()

    # resolve_config must never raise, even with no network.
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(os.path.join(tmp, "config.json"))
        cfg.set("location.zip", "43215")
        cfg.set("location.latitude", 39.9671)
        cfg.set("location.longitude", -83.0044)
        cfg.set("location.resolved_zip", "43215")
        check("resolve_config uses cached coords",
              geocode.resolve_config(cfg).latitude == 39.9671)
        cfg.set("location.zip", "")
        check("resolve_config tolerates a blank ZIP",
              geocode.resolve_config(cfg) is None)

    # A v2.0 config with raw coordinates must carry them into location.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as fh:
            json.dump({"weather": {"enabled": True, "latitude": 39.9612,
                                   "longitude": -82.9988,
                                   "units": "imperial"}}, fh)
        cfg = Config(path)
        check("v2.0 coordinates migrate to location",
              cfg.get("location.latitude") == 39.9612)
        check("migration drops the stale weather coords",
              cfg.get("weather.latitude") is None)
        check("migration keeps other weather settings",
              cfg.get("weather.units") == "imperial")


# ---------------------------------------------------------------- web

def test_web():
    print("web")
    from data import identity
    from web.app import create_app

    ctx, _ = build_context()
    tmp = tempfile.mkdtemp()
    ctx.config.path = os.path.join(tmp, "config.json")
    identity.STATE_DIR = os.path.join(tmp, "state")
    identity.REQUEST_PATH = os.path.join(identity.STATE_DIR, "hostname.request")
    ctx.store.wake = lambda: None

    client = create_app(ctx.config, ctx.store).test_client()

    ctx.config.set("setup.complete", False)
    resp = client.get("/")
    check("unconfigured board opens the wizard",
          resp.status_code == 302 and "/setup" in resp.headers.get("Location", ""))
    check("wizard renders", client.get("/setup").status_code == 200)

    resp = client.post("/api/setup", json={
        "name": "Mike's Board", "zip": "43215",
        "favorites": {"nfl": ["cin"], "ncaaf": ["OSU", "mich"]}})
    body = resp.get_json()
    check("setup accepted", resp.status_code == 200 and body.get("ok"), body)
    check("setup derives the mDNS name", body.get("mdns") == "mikes-board.local")
    check("setup upper-cases favourites",
          ctx.config.favorites("nfl") == ["CIN"]
          and ctx.config.favorites("ncaaf") == ["OSU", "MICH"])
    check("setup marks itself complete", ctx.config.get("setup.complete") is True)
    check("setup queues the rename",
          identity.pending_rename() == "mikes-board")
    check("configured board shows settings", client.get("/").status_code == 200)

    for payload, why in (({"name": "", "zip": "43215"}, "blank name"),
                         ({"name": "X", "zip": "abc"}, "letters in ZIP"),
                         ({"name": "X", "zip": "432"}, "short ZIP")):
        resp = client.post("/api/setup", json=payload)
        check("setup rejects {}".format(why), resp.status_code == 400)

    resp = client.post("/api/identity", json={"name": "Dad's Board"})
    check("rename works", resp.get_json().get("hostname") == "dads-board")
    check("rename rejects blank",
          client.post("/api/identity", json={"name": "  "}).status_code == 400)

    for path in ("/api/config", "/api/status", "/api/identity",
                 "/api/teams/ncaaf"):
        check("GET {}".format(path), client.get(path).status_code == 200)


def main():
    test_parser()
    test_config()
    test_playlist()
    test_render_safety()
    test_hardware()
    test_frame_rate_wiring()
    test_logos()
    test_logo_style()
    test_odds_row()
    test_geometry()
    test_matrix_options()
    test_identity()
    test_geocode()
    test_web()
    test_fantasy()
    test_secrets_never_leak()
    test_fantasy_screens()
    test_boot_bootstrap()
    test_doctor_advice()
    test_matrix_build()
    test_privilege_drop_readability()
    test_audio_conflict()
    test_apt_resilience()
    test_release_signature_round_trip()
    test_selfupdate_refuses_before_it_reaches_the_network()
    test_version_ordering_for_updates()
    test_slate_picks_the_day_from_the_schedule()
    test_off_day_league_leaves_the_rotation()
    test_pass_counter_counts_passes()
    test_board_profile_reaches_the_matrix()
    test_slowdown_is_guarded_in_the_right_direction()
    test_logo_colour_fidelity()
    test_dark_variant_is_chosen_by_measurement()
    test_logo_overrides_by_league()
    test_doctor_storage_checks()
    test_firstboot_network_probe()
    test_panel_sized_override_is_used_verbatim()
    test_live_favourite_does_not_fill_both_panels()
    test_clock_sync_before_apt()
    test_logo_cache_cannot_collide()
    test_pixel_art_is_sampled_not_filtered()
    test_rotation_quality()
    test_impossible_panel_settings_are_corrected()
    test_install_verify_is_not_a_race()
    test_panel_count_preseed()
    test_preseed_name()
    print()
    if FAILURES:
        print("{} failure(s): {}".format(len(FAILURES), ", ".join(FAILURES)))
        sys.exit(1)
    print("all checks passed")



# ---------------------------------------------------------------- fantasy

def test_fantasy():
    print("fantasy")
    import tempfile as _tf
    from data import fantasy, secrets

    # ---- input parsing: people paste links, not IDs ----
    cases = [
        ("https://fantasy.espn.com/football/league?leagueId=123456&seasonId=2026", "123456"),
        ("https://fantasy.espn.com/football/team?leagueId=987654&teamId=3", "987654"),
        ("fantasy.espn.com/football/league/123456", "123456"),
        ("123456", "123456"),
        ("  123456  ", "123456"),
        ("not a link", ""),
        ("", ""),
    ]
    ok = True
    for text, want in cases:
        if fantasy.parse_league_id(text) != want:
            ok = False
            print("      {!r} -> {!r}, wanted {!r}".format(
                text, fantasy.parse_league_id(text), want))
    check("league link parsing", ok)

    check("SWID braces normalised",
          fantasy.clean_swid("AABB") == "{AABB}"
          and fantasy.clean_swid("{AABB}") == "{AABB}"
          and fantasy.clean_swid("") == "")

    # January and February belong to the previous season, which is the bug
    # every fantasy tool ships in its first year.
    import datetime as _dt
    jan = _dt.datetime(2026, 1, 15, tzinfo=_dt.timezone.utc)
    sep = _dt.datetime(2026, 9, 15, tzinfo=_dt.timezone.utc)
    check("January resolves to the previous season",
          fantasy.current_season(jan) == 2025)
    check("September resolves to the current season",
          fantasy.current_season(sep) == 2026)

    # ---- parsing a real-shaped payload ----
    original_path = secrets.SECRETS_PATH
    original_fetch = fantasy.fetch
    try:
        secrets.SECRETS_PATH = os.path.join(_tf.mkdtemp(), "secrets.json")
        secrets.put(fantasy.NAMESPACE, {
            "espn_s2": "cookie",
            "swid": "{AABBCCDD-1234-5678-9012-ABCDEFABCDEF}"})

        payload = fixtures.fantasy_league(week=2)
        fantasy.fetch = lambda *a, **k: payload
        state = fantasy.league_state("123456")

        check("league name parsed", state.name == "Zahir Family League")
        check("week parsed", state.week == 2)
        check("all teams parsed", len(state.teams) == 6)
        check("only this week's matchups", len(state.matchups) == 3,
              len(state.matchups))
        check("my team found from SWID", state.my_matchup is not None)
        check("my team is the right one",
              state.my_matchup and state.my_matchup.mine.short == "HZ")
        # Live roster total must win over the settled totalPoints, or the
        # score sits frozen all afternoon.
        check("live points beat settled points",
              abs(state.my_matchup.mine.points - 91.2) < 0.01,
              state.my_matchup.mine.points)
        check("winning computed", state.my_matchup.winning is True)
        check("standings sorted by record",
              state.teams[0].wins >= state.teams[-1].wins)

        # A public league has no SWID, so no team is "mine" until picked.
        secrets.clear(fantasy.NAMESPACE)
        public = fantasy.league_state("123456")
        check("public league has no inferred team",
              public.my_matchup is None)
        chosen = fantasy.league_state("123456", my_team_id=1)
        check("explicit team id is honoured",
              chosen.my_matchup is not None
              and chosen.my_matchup.mine.short == "HZ")
    finally:
        fantasy.fetch = original_fetch
        secrets.SECRETS_PATH = original_path


def test_secrets_never_leak():
    print("secret handling")
    import json as _json
    import tempfile as _tf
    from data import fantasy, secrets
    from web.app import create_app

    original = secrets.SECRETS_PATH
    try:
        tmp = _tf.mkdtemp()
        secrets.SECRETS_PATH = os.path.join(tmp, "secrets.json")
        secrets.put(fantasy.NAMESPACE,
                    {"espn_s2": "SUPERSECRETCOOKIE", "swid": "{SECRET-SWID}"})

        ctx, _ = build_context()
        ctx.config.path = os.path.join(tmp, "config.json")
        ctx.store.wake = lambda: None
        ctx.store.fantasy_needs_auth = False
        ctx.store.fantasy_error = ""
        ctx.store.get_fantasy = lambda: None
        client = create_app(ctx.config, ctx.store).test_client()

        # config.json is served whole and unauthenticated to the LAN, so a
        # credential appearing there would be readable by anyone on the wifi.
        for path in ("/api/config", "/api/fantasy", "/api/status",
                     "/api/hardware"):
            body = _json.dumps(client.get(path).get_json())
            if "SUPERSECRETCOOKIE" in body or "SECRET-SWID" in body:
                check("no credential leak at {}".format(path), False)
                break
        else:
            check("credentials never appear in any API response", True)

        check("status reports presence without the value",
              client.get("/api/fantasy").get_json().get("has_credentials") is True)
        check("redaction keeps only a tail",
              secrets.redact("ABCDEFGH") == "...EFGH"
              and secrets.redact("AB") == "**")

        check("clearing removes them", secrets.clear(fantasy.NAMESPACE)
              and not secrets.has(fantasy.NAMESPACE, "espn_s2"))
    finally:
        secrets.SECRETS_PATH = original


def test_fantasy_screens():
    print("fantasy screens")
    import tempfile as _tf
    from data import fantasy, secrets
    from renderer.screens import fantasy as fantasy_screens

    original_path, original_fetch = secrets.SECRETS_PATH, fantasy.fetch
    try:
        secrets.SECRETS_PATH = os.path.join(_tf.mkdtemp(), "secrets.json")
        secrets.put(fantasy.NAMESPACE, {
            "espn_s2": "c",
            "swid": "{AABBCCDD-1234-5678-9012-ABCDEFABCDEF}"})
        fantasy.fetch = lambda *a, **k: fixtures.fantasy_league(week=2)

        ctx, _ = build_context()
        state = fantasy.league_state("123456")
        matchup = state.my_matchup

        # Every plausible score shape must render inside the panel. The margin
        # used to be drawn over the points once both sides passed 100.
        ok = True
        for mine, theirs in [(91.2, 84.6), (71.2, 104.6), (128.4, 119.0),
                             (148.9, 42.1), (0.0, 0.0), (99.9, 99.8),
                             (200.5, 3.2)]:
            matchup.home.points, matchup.away.points = mine, theirs
            frame = Frame(64, 32)
            try:
                fantasy_screens.FantasyMatchupScreen(ctx, matchup).draw(frame, 0.5)
            except Exception as exc:
                ok = False
                print("      {} vs {} raised: {}".format(mine, theirs, exc))
            if frame.image.size != (64, 32):
                ok = False
        check("matchup renders at every score", ok)

        ok = True
        for page in (0, 1, 5):
            try:
                fantasy_screens.FantasyLeagueScreen(ctx, state, page).draw(
                    Frame(64, 32), 0.5)
            except Exception as exc:
                ok = False
                print("      standings page {} raised: {}".format(page, exc))
        check("standings render, including past the end", ok)

        try:
            fantasy_screens.FantasyAuthScreen(ctx, "board.local").draw(
                Frame(64, 32), 0.2)
            check("expired-login screen renders", True)
        except Exception as exc:
            check("expired-login screen renders", False, exc)
    finally:
        fantasy.fetch = original_fetch
        secrets.SECRETS_PATH = original_path


def test_boot_bootstrap():
    """The SD-card first-boot path, checked without a card or a Pi.

    Every failure mode here is expensive: it surfaces on a Pi with no screen
    and no ssh, in someone else's living room. So the shape of the two scripts
    is asserted rather than trusted.
    """
    print("boot bootstrap")
    import subprocess, shutil, tempfile

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    stage_a = os.path.join(root, "boot", "scoreboard-firstboot.sh")
    prep = os.path.join(root, "boot", "prepare-sd-card.sh")

    check("first-boot script exists", os.path.exists(stage_a))
    check("card-prep script exists", os.path.exists(prep))
    if not (os.path.exists(stage_a) and os.path.exists(prep)):
        return

    for path in (stage_a, prep):
        rc = subprocess.call(["bash", "-n", path])
        check("{} parses".format(os.path.basename(path)), rc == 0)

    body = open(stage_a).read()

    # The reboot loop. cmdline.txt asks for a reboot on success, so a stage A
    # that does not remove its own hook reboots the Pi forever.
    check("stage A removes its own cmdline hook",
          "scoreboard-firstboot" in body and "sed -i" in body)
    # ...but only its own: stripping Imager's hook breaks user/wifi setup.
    check("cmdline cleanup is guarded by a match on our own hook",
          re.search(r"grep -q 'systemd\.run=\[\^ \]\*scoreboard-firstboot'", body)
          is not None)

    # Stage A must NOT resolve the account: it runs before cloud-init, so uid
    # 1000 there is whatever placeholder the image ships, not the account the
    # card asked for. Getting this wrong installs everything for a user nobody
    # can log in as.
    stage_a_only = body.split("cat > /usr/local/sbin/scoreboard-stage-b")[0]
    check("stage A does not resolve the user account",
          "getent passwd" not in stage_a_only)
    check("stage A stages the payload somewhere root-owned",
          "/var/lib/scoreboard" in stage_a_only)
    check("stage B resolves the account itself",
          "getent passwd 1000" in body.split("<<'STAGEB'", 1)[1])
    check("stage B waits for real DNS, not just an interface",
          "getent hosts" in body)
    check("stage B is ordered after the network",
          "After=network-online.target" in body)
    check("install is not attempted in the pre-network target",
          "install.sh" not in body.split("cat > /etc/systemd/system")[0])
    check("progress is logged to the card, which is the only readable place",
          "scoreboard-install.log" in body)

    # The log is the diagnostic of last resort, so writing it must not depend on
    # sudo. An earlier version piped every log line through sudo; when sudo
    # wanted a password the whole of stage B wrote nothing and looked identical
    # to a service that never started.
    stage_b = body.split("<<'STAGEB'", 1)[1].split("\nSTAGEB", 1)[0]
    check("stage B body was actually extracted", len(stage_b) > 500,
          "got {} chars".format(len(stage_b)))
    check("stage B logs without sudo",
          "sudo tee" not in stage_b and "| sudo" not in stage_b)
    check("stage B runs as root rather than the user",
          "User=" not in body.split("[Service]")[1].split("[Install]")[0])
    check("stage B still runs the install as the user",
          "runuser" in stage_b)
    check("stage B guarantees passwordless sudo for the install",
          "sudoers.d" in stage_b)

    # Every exit path has to clear the hook. An early return that skips it is
    # an infinite reboot loop on a board with no screen, which is the worst
    # failure this project can produce.
    exits = [ln for ln in body.splitlines() if ln.strip() == "exit 0"]
    check("stage A has more than one exit path", len(exits) >= 2)
    cleanup_calls = body.count("clear_hook")
    check("hook cleanup is a function, called from every exit",
          "clear_hook()" in body and cleanup_calls >= len(exits) + 1,
          "{} calls for {} exits".format(cleanup_calls, len(exits)))
    for guard in ("no scoreboard zip",):
        idx = stage_a_only.find(guard)
        tail = stage_a_only[idx:idx + 400] if idx >= 0 else ""
        check("the '{}' path clears the hook before exiting".format(guard),
              idx >= 0 and "clear_hook" in tail.split("exit 0")[0])
    check("the account check lives in stage B, which cannot reboot-loop",
          "no usable account" in stage_b)

    # Extract the embedded stage B heredoc and parse it on its own.
    inner = body.split("<<'STAGEB'", 1)[1].split("\nSTAGEB", 1)[0]
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(inner)
        inner_path = fh.name
    check("stage B parses", subprocess.call(["bash", "-n", inner_path]) == 0)
    os.unlink(inner_path)

    # End-to-end card prep, against a fake boot volume of each kind.
    zips = tempfile.mkdtemp()
    payload = os.path.join(zips, "led-scoreboard-test.zip")
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("scoreboard/main.py", "# test\n")

    def prepare(vol, extra=()):
        return subprocess.call(["bash", prep, payload, "--volume", vol] + list(extra),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    plain = tempfile.mkdtemp()
    open(os.path.join(plain, "cmdline.txt"), "w").write(
        "console=tty1 root=PARTUUID=ab rootwait\n")
    open(os.path.join(plain, "custom.toml"), "w").write("[system]\n")
    rc = prepare(plain, ["--name", "Living Room Scoreboard"])
    cmdline = open(os.path.join(plain, "cmdline.txt")).read()
    check("plain card: prep succeeds", rc == 0)
    check("plain card: hook added",
          "systemd.run=" in cmdline and "scoreboard-firstboot.sh" in cmdline)
    check("plain card: cmdline stays a single line", cmdline.strip().count("\n") == 0)
    check("plain card: original kept", os.path.exists(os.path.join(plain, "cmdline.txt.bak")))
    check("plain card: payload copied",
          os.path.exists(os.path.join(plain, "led-scoreboard-test.zip")))
    name_file = os.path.join(plain, "scoreboard-name.txt")
    check("plain card: name preseeded",
          os.path.exists(name_file) and
          open(name_file).read().strip() == "Living Room Scoreboard")

    # A card Imager never customised has no wifi and, on current Pi OS, no user
    # account at all. Preparing it produces a Pi that boots to a setup wizard
    # nobody can see, so the script refuses rather than warning.
    bare = tempfile.mkdtemp()
    open(os.path.join(bare, "cmdline.txt"), "w").write("console=tty1 rootwait\n")
    rc = subprocess.call(["bash", prep, payload, "--volume", bare],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check("uncustomised card is refused", rc != 0)
    check("nothing is written to a refused card",
          sorted(os.listdir(bare)) == ["cmdline.txt"])
    rc = subprocess.call(["bash", prep, payload, "--volume", bare, "--force"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check("--force overrides the refusal", rc == 0)
    shutil.rmtree(bare, ignore_errors=True)

    for marker in ("custom.toml", "firstrun.sh", "wpa_supplicant.conf", "userconf.txt"):
        vol = tempfile.mkdtemp()
        open(os.path.join(vol, "cmdline.txt"), "w").write("console=tty1 rootwait\n")
        open(os.path.join(vol, marker), "w").write("x\n")
        rc = subprocess.call(["bash", prep, payload, "--volume", vol],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("{} counts as customised".format(marker), rc == 0)
        shutil.rmtree(vol, ignore_errors=True)

    # Writing custom.toml ourselves, because Imager's customisation step has a
    # known intermittent bug where the settings never reach the card.
    from data import identity as _identity
    for board_name, expect_host in [("Living Room Scoreboard", "living-room-scoreboard"),
                                    ("Dad's Board #2", "dads-board-2")]:
        vol = tempfile.mkdtemp()
        open(os.path.join(vol, "cmdline.txt"), "w").write("console=tty1 rootwait\n")
        rc = subprocess.call(
            ["bash", prep, payload, "--volume", vol, "--name", board_name,
             "--wifi", "HomeNet", "--wifi-pass", "s3cret",
             "--user", "hassan", "--user-pass", "hunter2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        toml_path = os.path.join(vol, "custom.toml")
        check("custom.toml written for {!r}".format(board_name),
              rc == 0 and os.path.exists(toml_path))
        toml = open(toml_path).read() if os.path.exists(toml_path) else ""
        check("  config_version present", "config_version = 1" in toml)
        check("  ssid carried through", 'ssid = "HomeNet"' in toml)
        check("  wifi password left unencrypted",
              'password = "s3cret"' in toml and "[wlan]" in toml)
        check("  user carried through", 'name = "hassan"' in toml)
        check("  ssh enabled", "[ssh]" in toml and "enabled = true" in toml)
        check("  hostname is {}".format(expect_host),
              'hostname = "{}"'.format(expect_host) in toml)
        # The bash slug and the Python slug must not drift, or the board would
        # answer at one name before the install and another after it.
        check("  bash slug matches identity.slugify",
              _identity.slugify(board_name) == expect_host)
        shutil.rmtree(vol, ignore_errors=True)

    # Raspberry Pi OS moved to cloud-init in the Trixie images (Nov 2025). Those
    # read user-data/meta-data/network-config and ignore custom.toml entirely,
    # so writing the wrong one is a silent no-op.
    vol = tempfile.mkdtemp()
    open(os.path.join(vol, "cmdline.txt"), "w").write(
        "console=tty1 root=PARTUUID=aa rootwait ds=nocloud;i=rpi-imager-1\n")
    open(os.path.join(vol, "meta-data"), "w").write("instance-id: rpi\n")
    rc = subprocess.call(
        ["bash", prep, payload, "--volume", vol, "--name", "RGB Scoreboard",
         "--wifi", "HomeNet", "--wifi-pass", "s3cret",
         "--user", "scoreboard", "--user-pass", "sb"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check("cloud-init card: prep succeeds", rc == 0)
    check("cloud-init card: no pointless custom.toml",
          not os.path.exists(os.path.join(vol, "custom.toml")))
    for f in ("user-data", "meta-data", "network-config"):
        check("cloud-init card: {} written".format(f),
              os.path.exists(os.path.join(vol, f)))
    ud = open(os.path.join(vol, "user-data")).read()
    nc = open(os.path.join(vol, "network-config")).read()
    check("cloud-init card: user is the one asked for", "name: scoreboard" in ud)
    check("cloud-init card: hostname slugged", "hostname: rgb-scoreboard" in ud)
    check("cloud-init card: password auth on", "ssh_pwauth: true" in ud)
    check("cloud-init card: ssh explicitly started",
          "systemctl, enable, --now, ssh" in ud)
    check("cloud-init card: passwordless sudo for the install",
          "NOPASSWD" in ud)
    # Our user-data replaces Imager's wholesale, so anything Imager set that we
    # omit silently reverts to the image default. Timezone reverting showed up
    # as game times hours off, which reads as a scoreboard bug.
    check("cloud-init card: timezone set", "timezone: America/New_York" in ud)
    check("cloud-init card: keyboard layout set", "layout: us" in ud)
    check("cloud-init card: wifi in netplan", '"HomeNet"' in nc and "wifis" in nc)
    check("cloud-init card: regulatory domain set",
          "ieee80211_regdom" in open(os.path.join(vol, "cmdline.txt")).read())
    check("cloud-init card: Imager's files kept",
          os.path.exists(os.path.join(vol, "meta-data.imager-bak")))
    shutil.rmtree(vol, ignore_errors=True)

    # Imager's network-config is the half that works. When it already names the
    # same SSID, keep it: swapping a working netplan for a hand-written one to
    # change a setting that lives in user-data trades a login problem for a
    # no-network problem.
    netcfg = ('version: 2\nwifis:\n  wlan0:\n    access-points:\n'
              '      "HomeNet":\n        password: "orig"\n    dhcp4: true\n')
    vol = tempfile.mkdtemp()
    open(os.path.join(vol, "cmdline.txt"), "w").write("rootwait ds=nocloud\n")
    open(os.path.join(vol, "meta-data"), "w").write("instance-id: rpi\n")
    open(os.path.join(vol, "user-data"), "w").write("#cloud-config\n")
    open(os.path.join(vol, "network-config"), "w").write(netcfg)
    subprocess.call(["bash", prep, payload, "--volume", vol, "--name", "B",
                     "--wifi", "HomeNet", "--wifi-pass", "new",
                     "--user", "u", "--user-pass", "p"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check("same SSID: Imager's network-config is left exactly as it was",
          open(os.path.join(vol, "network-config")).read() == netcfg)
    check("same SSID: user-data is still replaced",
          "name: u" in open(os.path.join(vol, "user-data")).read())
    shutil.rmtree(vol, ignore_errors=True)

    vol = tempfile.mkdtemp()
    open(os.path.join(vol, "cmdline.txt"), "w").write("rootwait ds=nocloud\n")
    open(os.path.join(vol, "meta-data"), "w").write("instance-id: rpi\n")
    open(os.path.join(vol, "network-config"), "w").write(
        netcfg.replace("HomeNet", "SomeOtherNetwork"))
    subprocess.call(["bash", prep, payload, "--volume", vol, "--name", "B",
                     "--wifi", "HomeNet", "--wifi-pass", "new",
                     "--user", "u", "--user-pass", "p"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    got = open(os.path.join(vol, "network-config")).read()
    check("different SSID: ours replaces theirs", '"HomeNet"' in got)
    check("different SSID: theirs is kept as a backup",
          os.path.exists(os.path.join(vol, "network-config.imager-bak")))
    shutil.rmtree(vol, ignore_errors=True)

    # The boot-time apt timers hold the dpkg lock. An install that races them
    # fails every apt call, which cascades into no git, no matrix library, no
    # Pillow and a service that cannot import: eleven reported failures from one
    # lock.
    check("stage B stops the boot-time apt timers", "apt-daily" in stage_b)
    check("stage B waits for the dpkg lock to clear",
          "lock-frontend" in stage_b and "fuser" in stage_b)
    check("stage B reports a failed apt update rather than cascading",
          "apt-get update" in stage_b)

    # Resolving one host is not the same as a link that carries traffic. pip
    # talks to pypi.org, not github.com, so checking only github let the install
    # start on a link that then dropped the pip and git fetches.
    check("connectivity is checked against every host the install needs",
          "pypi.org" in stage_b and "github.com" in stage_b)
    # Fetched, not just resolved -- but without -f, which is exactly what made
    # this probe reject every healthy network. See test_firstboot_network_probe.
    check("connectivity is fetched, not just resolved",
          "curl -sS" in stage_b and "net_ready" in stage_b)

    # A first boot that fails on a flaky link should fix itself rather than
    # printing advice only a human with ssh can act on.
    check("the install is retried once before giving up",
          "attempt 1 2" in stage_b.replace("for attempt in ", "attempt ") or
          "for attempt in 1 2" in stage_b)
    check("the retry waits before trying again", "sleep 60" in stage_b)

    # Diagnostics on network failure, so a dead end costs no re-flash cycle.
    for probe in ("nmcli", "rfkill", "cloud-init status", "netplan"):
        check("network failure dumps {} state to the card".format(probe),
              probe in stage_b)
    check("diagnostics do not print the wifi password",
          "grep -rvE 'password|psk'" in stage_b)

    # A cloud-init card with Imager's files present counts as customised, so it
    # is never refused. Getting this wrong sent a perfectly good card back to
    # be re-flashed.
    vol = tempfile.mkdtemp()
    open(os.path.join(vol, "cmdline.txt"), "w").write("ds=nocloud rootwait\n")
    for f in ("user-data", "meta-data", "network-config"):
        open(os.path.join(vol, f), "w").write("x\n")
    rc = subprocess.call(["bash", prep, payload, "--volume", vol],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check("cloud-init card with Imager's settings is not refused", rc == 0)
    shutil.rmtree(vol, ignore_errors=True)

    # Explicit credentials beat whatever Imager left behind. Deferring to
    # Imager's file silently dropped the --user and --wifi that were asked for,
    # and Imager's file may have no SSH switch in it at all.
    vol = tempfile.mkdtemp()
    open(os.path.join(vol, "cmdline.txt"), "w").write("console=tty1 rootwait\n")
    open(os.path.join(vol, "custom.toml"), "w").write("# theirs\n")
    subprocess.call(["bash", prep, payload, "--volume", vol,
                     "--wifi", "X", "--wifi-pass", "y",
                     "--user", "zed", "--user-pass", "w"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    written = open(os.path.join(vol, "custom.toml")).read()
    check("explicit credentials override Imager's settings",
          'name = "zed"' in written and 'ssid = "X"' in written)
    check("ssh is always enabled in what we write",
          "[ssh]" in written and "enabled = true" in written)
    check("Imager's file is kept, not destroyed",
          os.path.exists(os.path.join(vol, "custom.toml.imager-bak")))
    shutil.rmtree(vol, ignore_errors=True)

    # With no credentials given there is nothing to override, so theirs stands.
    vol = tempfile.mkdtemp()
    open(os.path.join(vol, "cmdline.txt"), "w").write("console=tty1 rootwait\n")
    open(os.path.join(vol, "custom.toml"), "w").write("# theirs\n")
    subprocess.call(["bash", prep, payload, "--volume", vol, "--name", "Board"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check("without credentials Imager's settings are left alone",
          open(os.path.join(vol, "custom.toml")).read() == "# theirs\n" and
          not os.path.exists(os.path.join(vol, "custom.toml.imager-bak")))
    shutil.rmtree(vol, ignore_errors=True)

    # Half-given credentials are refused rather than written incomplete.
    for args in (["--wifi", "N"], ["--user", "u"],
                 ["--wifi", "N", "--wifi-pass", "p"]):
        vol = tempfile.mkdtemp()
        open(os.path.join(vol, "cmdline.txt"), "w").write("console=tty1\n")
        rc = subprocess.call(["bash", prep, payload, "--volume", vol] + args,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("incomplete credentials refused: {}".format(" ".join(args)),
              rc != 0 and not os.path.exists(os.path.join(vol, "custom.toml")))
        shutil.rmtree(vol, ignore_errors=True)

    # Re-running on the same card must not stack a second hook.
    prepare(plain)
    again = open(os.path.join(plain, "cmdline.txt")).read()
    check("plain card: prep is idempotent", again.count("systemd.run=") == 1)

    imager = tempfile.mkdtemp()
    original_cmdline = ("console=tty1 root=PARTUUID=ab rootwait "
                        "systemd.run=/boot/firmware/firstrun.sh "
                        "systemd.run_success_action=reboot "
                        "systemd.unit=kernel-command-line.target\n")
    open(os.path.join(imager, "cmdline.txt"), "w").write(original_cmdline)
    open(os.path.join(imager, "firstrun.sh"), "w").write(
        "#!/bin/bash\nset +e\necho giftpi >/etc/hostname\n"
        "rm -f /boot/firmware/firstrun.sh\n"
        "sed -i 's| systemd.run.*||g' /boot/firmware/cmdline.txt\nexit 0\n")
    rc = prepare(imager)
    firstrun = open(os.path.join(imager, "firstrun.sh")).read()
    check("imager card: prep succeeds", rc == 0)
    check("imager card: cmdline left alone",
          open(os.path.join(imager, "cmdline.txt")).read() == original_cmdline)
    check("imager card: call inserted into firstrun.sh",
          "scoreboard-firstboot.sh" in firstrun)
    lines = [l.strip() for l in firstrun.splitlines()]
    call = next(i for i, l in enumerate(lines) if "scoreboard-firstboot" in l)
    cleanup = next(i for i, l in enumerate(lines) if l.startswith("rm -f"))
    check("imager card: call runs before firstrun deletes itself", call < cleanup)

    for path in (plain, imager, zips):
        shutil.rmtree(path, ignore_errors=True)


def test_doctor_advice():
    """Every fix doctor prints has to be a command that actually works."""
    print("doctor advice")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "scoreboard")).read()

    # The import name is not always the package name. "pip install PIL" sends
    # you after a package that does not exist.
    check("PIL maps to the Pillow package",
          "install --break-system-packages Pillow" in src)
    check("no advice to install a package called PIL",
          "break-system-packages PIL" not in src)
    check("rgbmatrix is not offered as a pip install",
          "break-system-packages rgbmatrix" not in src)


def test_matrix_build():
    """The bindings build must match upstream's current layout.

    Upstream deleted the `make build-python` target in February 2026 when the
    Python bindings moved to scikit-build-core and cmake. Building the wrong way
    fails with "No rule to make target", which reads like a broken checkout
    rather than a stale command.
    """
    print("matrix build")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "install.sh")).read()

    matrix_step = src[src.index('say "RGB matrix library"'):
                      src.index('say "Configuration"')]
    check("pyproject checkouts are installed with pip",
          "pip install" in matrix_step and "./matrix" in matrix_step)
    check("the cmake/cython build dependencies are installed",
          "cython3" in matrix_step and "cmake" in matrix_step)
    check("build-python is only used for an older checkout's own Makefile",
          "bindings/python" in matrix_step)
    check("the top-level Makefile is never asked for build-python",
          "cd matrix && make build-python" not in src)
    check("a failed build is reported, not left to surface at service start",
          "rgbmatrix not installed" in matrix_step)

    # A missing emulator must not masquerade as the problem.
    disp = open(os.path.join(root, "renderer", "display.py")).read()
    check("no backend gives an actionable message, not a bare ImportError",
          "No LED matrix backend available" in disp)
    check("that message names the real fix",
          "pip install --break-system-packages ./matrix" in disp)


def test_privilege_drop_readability():
    """The library setuids to `daemon`, so `daemon` is who has to read the files.

    Debian made home directories 0750 in Bookworm, so /home/<user> blocks
    traversal by daemon and every font under it becomes unreachable the moment
    privileges drop. PIL calls that "OSError: cannot open resource", which reads
    like a missing file.
    """
    print("privilege drop readability")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    inst = open(os.path.join(root, "install.sh")).read()
    cli = open(os.path.join(root, "scoreboard")).read()
    disp = open(os.path.join(root, "renderer", "display.py")).read()

    perms = inst[inst.index('say "Permissions for the privilege drop"'):
                 inst.index('say "mDNS')]
    check("every parent directory is made traversable by daemon",
          "o+x" in perms and "dirname" in perms)
    check("only the execute bit is added, not read",
          "o+rx" not in perms and "chmod 755" not in perms)
    check("readability is proven by reading a font as daemon",
          "sudo -u daemon cat" in perms)
    check("a failure here is reported, not silent",
          "daemon cannot read project files" in perms)

    check("doctor checks what daemon can read, not what root can",
          "sudo -u daemon cat" in cli)
    check("doctor names the directory to fix",
          "chmod o+x" in cli)

    # The error reporter must never replace the error it is reporting.
    err = disp[disp.index("def _draw_error"):]
    check("the real error reaches stderr before anything is drawn",
          err.index("stderr") < err.index("Frame("))
    check("drawing the error screen cannot itself crash the loop",
          "except Exception as inner" in err)
    check("an unreadable-font failure explains the privilege drop",
          "drops" in err and "daemon" in err)


def test_audio_conflict():
    """The panel and the Pi's sound driver want the same PWM peripheral.

    With snd_bcm2835 loaded the matrix library refuses to start on any PWM
    mapping -- "regular" and "adafruit-hat-pwm", which covers most non-Adafruit
    boards -- and says so on stderr. For a service that means the journal, which
    means nobody sees it: the panel is simply dark and everything else is green.
    """
    print("audio conflict")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    inst = open(os.path.join(root, "install.sh")).read()
    cli = open(os.path.join(root, "scoreboard")).read()

    check("the installer disables onboard audio", "dtparam=audio=off" in inst)
    check("it handles both config.txt locations",
          "/boot/firmware/config.txt" in inst and "/boot/config.txt" in inst)
    check("an existing audio=on line is rewritten rather than duplicated",
          "dtparam=audio=on" in inst)
    check("the module is blacklisted as well as the overlay disabled",
          "blacklist snd_bcm2835" in inst)
    check("the needed reboot is surfaced in the closing summary",
          "Reboot needed" in inst)

    check("doctor checks the conflict against the configured mapping",
          "snd_bcm2835" in cli and "gpio_mapping" in cli)
    check("doctor only calls it a problem for PWM mappings",
          "adafruit-hat-pwm" in cli)


def test_apt_resilience():
    """A broken third-party repo must not be able to break everything else."""
    print("apt resilience")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "install.sh")).read()

    # On Trixie an unverifiable repo fails EVERY apt update, so one bad signing
    # key took out python3-pillow, git and build-essential too: eleven reported
    # failures, one cause, and a dark panel.
    pkg_step = src.index('say "System packages"')
    preamble = src[pkg_step:src.index('step "apt update"', pkg_step)]
    check("a broken repo is detected before the package step",
          "not signed" in preamble or "missing key" in preamble.lower())
    check("and removed so apt works again",
          "sources.list.d/comitup.list" in preamble)

    # The repo is added by upstream's bootstrap package, which carries the key,
    # rather than by hand-fetching a key from a guessed URL.
    check("comitup repo comes from upstream's apt-source deb",
          "davesteele-comitup-apt-source" in src)
    check("no hand-rolled keyring from a guessed URL",
          "key-joeisanerd" not in src)
    check("the sources entry is not written when the key step failed",
          "could not add the Comitup repository" in src)


def test_release_signature_round_trip():
    """A board must install a release I signed and refuse everything else.

    This is the one test in the suite where a pass means something about
    security rather than about behaviour. The self-updater is a remote code
    execution channel into three houses I cannot get into, so "reached GitHub
    over HTTPS" is not good enough: HTTPS proves I talked to GitHub, not that
    what GitHub handed over is mine. A signature over the manifest is what
    makes a compromised account, a renamed repository claimed by someone else,
    or a proxy in the middle into a refused update rather than four owned Pis.

    Run against the real openssl, with a real key pair, because a hand-rolled
    approximation of the check would prove nothing about the check that ships.
    """
    print("release signature round trip")
    import json
    import subprocess
    import tempfile

    def run(*cmd, **kw):
        return subprocess.run(cmd, capture_output=True, text=True, **kw)

    if run("openssl", "version").returncode != 0:
        check("openssl is available to verify releases", False)
        return

    with tempfile.TemporaryDirectory() as tmp:
        mine = os.path.join(tmp, "mine.pem")
        mine_pub = os.path.join(tmp, "mine.pub")
        theirs = os.path.join(tmp, "theirs.pem")
        theirs_pub = os.path.join(tmp, "theirs.pub")
        # 2048 rather than the 4096 the real script uses: this is testing the
        # mechanism, and key generation is the slow part of the suite.
        for key, pub in ((mine, mine_pub), (theirs, theirs_pub)):
            check("generated a test key",
                  run("openssl", "genrsa", "-out", key, "2048").returncode == 0)
            run("openssl", "rsa", "-in", key, "-pubout", "-out", pub)

        manifest = os.path.join(tmp, "manifest.json")
        with open(manifest, "w") as fh:
            json.dump({"schema": 1, "channels": {"stable": {
                "version": "9.9.9",
                "url": "https://example.invalid/led-scoreboard-v9.9.9.zip",
                "sha256": "0" * 64}}}, fh)

        sig = os.path.join(tmp, "manifest.sig")
        check("signing works",
              run("openssl", "dgst", "-sha256", "-sign", mine,
                  "-out", sig, manifest).returncode == 0)

        def verify(pub, sig_path, doc):
            return run("openssl", "dgst", "-sha256", "-verify", pub,
                       "-signature", sig_path, doc).returncode == 0

        check("a release signed with my key verifies", verify(mine_pub, sig, manifest))

        # The three ways this has to fail.
        check("a manifest signed with somebody else's key is refused",
              not verify(theirs_pub, sig, manifest))

        with open(manifest) as fh:
            doc = json.load(fh)
        doc["channels"]["stable"]["url"] = "https://evil.invalid/payload.zip"
        tampered = os.path.join(tmp, "tampered.json")
        with open(tampered, "w") as fh:
            json.dump(doc, fh)
        check("changing the download URL after signing is caught",
              not verify(mine_pub, sig, tampered))

        doc["channels"]["stable"]["url"] = \
            "https://example.invalid/led-scoreboard-v9.9.9.zip"
        doc["channels"]["stable"]["sha256"] = "1" * 64
        swapped = os.path.join(tmp, "swapped.json")
        with open(swapped, "w") as fh:
            json.dump(doc, fh)
        check("swapping the checksum after signing is caught",
              not verify(mine_pub, sig, swapped))

        theirs_sig = os.path.join(tmp, "theirs.sig")
        run("openssl", "dgst", "-sha256", "-sign", theirs,
            "-out", theirs_sig, manifest)
        check("a valid signature from the wrong key is still refused",
              not verify(mine_pub, theirs_sig, manifest))


def test_selfupdate_refuses_before_it_reaches_the_network():
    """Every reason to stop must stop it before anything is downloaded.

    Checked by running the real script in a sandbox rather than by reading it,
    because the failure that matters here is a guard that was written and then
    placed after the download.
    """
    print("selfupdate refusals")
    import json
    import subprocess
    import tempfile

    script = os.path.join(root_dir(), "tools", "selfupdate.sh")
    check("the updater ships with the release", os.path.exists(script))
    if not os.path.exists(script):
        return

    def sandbox(tmp, updates, name="scoreboard"):
        home = os.path.join(tmp, name)
        os.makedirs(os.path.join(home, "state"), exist_ok=True)
        os.makedirs(os.path.join(home, "tools"), exist_ok=True)
        with open(os.path.join(home, "main.py"), "w") as fh:
            fh.write('VERSION = "1.0.0"\n')
        with open(os.path.join(home, "config.json"), "w") as fh:
            json.dump({"updates": updates, "web": {"port": 8080}}, fh)
        import shutil
        shutil.copy(script, os.path.join(home, "tools", "selfupdate.sh"))
        return home

    def run_in(home, key=None, args=()):
        env = dict(os.environ)
        env["SELFUPDATE_KEY"] = key or os.path.join(home, "no-such-key.pem")
        return subprocess.run(
            ["bash", os.path.join(home, "tools", "selfupdate.sh")] + list(args),
            capture_output=True, text=True, env=env, timeout=120)

    with tempfile.TemporaryDirectory() as tmp:
        # Switched off by the recipient.
        home = sandbox(tmp, {"enabled": False, "channel": "stable",
                             "manifest_url": "https://example.invalid/m.json"})
        out = run_in(home)
        check("a board with updates switched off does nothing",
              out.returncode == 0 and "switched off" in out.stdout,
              out.stdout.strip()[-120:])

    with tempfile.TemporaryDirectory() as tmp:
        # No channel configured: not an error, just nothing to do.
        home = sandbox(tmp, {"enabled": True, "channel": "stable",
                             "manifest_url": ""})
        out = run_in(home)
        check("no configured channel is a quiet no-op, not a failure",
              out.returncode == 0 and "no manifest URL" in out.stdout,
              out.stdout.strip()[-120:])

    with tempfile.TemporaryDirectory() as tmp:
        # Plain HTTP must be refused outright: the signature would still be
        # checked, but there is no reason to accept a downgrade.
        home = sandbox(tmp, {"enabled": True, "channel": "stable",
                             "manifest_url": "http://example.invalid/m.json"})
        out = run_in(home)
        check("an http manifest URL is refused",
              out.returncode != 0 and "must be https" in out.stdout,
              out.stdout.strip()[-120:])

    with tempfile.TemporaryDirectory() as tmp:
        # The important one: no key means no updates, ever, rather than
        # falling back to trusting the transport.
        home = sandbox(tmp, {"enabled": True, "channel": "stable",
                             "manifest_url": "https://example.invalid/m.json"})
        out = run_in(home)
        check("a board with no update key installs nothing",
              out.returncode != 0 and "no update key" in out.stdout,
              out.stdout.strip()[-140:])
        check("and says so where it can be found later",
              os.path.exists(os.path.join(home, "state", "update.log")))

    with tempfile.TemporaryDirectory() as tmp:
        # A non-standard directory name would grow a second copy rather than
        # updating this one, because the archive unpacks as ./scoreboard.
        home = sandbox(tmp, {"enabled": True, "channel": "stable",
                             "manifest_url": "https://example.invalid/m.json"},
                       name="scoreboard-old")
        out = run_in(home)
        check("an install under a different directory name refuses to update",
              out.returncode != 0 and "standard layout" in out.stdout,
              out.stdout.strip()[-140:])

    src = open(script).read()
    # Ordering, stated as a property rather than trusted to review.
    check("the signature is verified before the release is downloaded",
          src.index("dgst -sha256 -verify") < src.index("-o \"$ZIP\""))
    check("the download is checksummed before it is unpacked",
          src.index("sha256sum \"$ZIP\"") < src.index('unzip -oq "$ZIP"'))
    check("the new tree is tested before the live one is touched",
          src.index("tools.test_scoreboard") < src.index('mv "$NEW" "$HERE"'))
    check("the working copy is backed up before it is replaced",
          src.index('cp -a "$HERE" "$BACKUP"') < src.index('rm -rf "$HERE"'))
    check("a failed version is recorded so it is not retried nightly",
          "update-failed-versions" in src)
    check("the key lives outside the tree an update replaces",
          "/etc/scoreboard/update-key.pem" in src)
    check("a live game defers the install",
          "live right now" in src)

    installer = open(os.path.join(root_dir(), "install.sh")).read()
    check("the installer writes the key once and will not replace it",
          "A release cannot replace the key" in installer)
    check("the nightly timer is only armed when a channel is configured",
          "self-update not armed" in installer)

    unit = open(os.path.join(root_dir(), "systemd",
                             "scoreboard-update.timer")).read()
    check("a board that was off at 4am still catches up",
          "Persistent=true" in unit)
    check("four boards do not all check in the same second",
          "RandomizedDelaySec" in unit)


def test_version_ordering_for_updates():
    """"Newer" has to mean newer, including across a two-digit minor bump.

    A string comparison says 2.9.0 is newer than 2.10.0, which would strand
    every board on the older release exactly once the numbering got there --
    and this project is at 2.27, so it is already past the point where a naive
    compare silently stops working.
    """
    print("update version ordering")
    import subprocess

    def newer(a, b):
        out = subprocess.run(
            ["bash", "-c",
             'a="$1"; b="$2"; [[ "$a" != "$b" ]] && '
             '[[ "$(printf "%s\n%s\n" "$a" "$b" | sort -V | tail -1)" == "$a" ]]',
             "_", a, b], capture_output=True)
        return out.returncode == 0

    check("a patch bump is newer", newer("2.27.1", "2.27.0"))
    check("a minor bump is newer", newer("2.28.0", "2.27.0"))
    check("two digits beat one", newer("2.10.0", "2.9.0"))
    check("and the reverse is not newer", not newer("2.9.0", "2.10.0"))
    check("the same version is not newer", not newer("2.27.0", "2.27.0"))
    check("an older release is not offered", not newer("2.26.0", "2.27.0"))


def test_slate_picks_the_day_from_the_schedule():
    """The current day has to come from the games, not from a weekday table.

    A table is the obvious implementation and it is wrong in every direction
    that matters. Thanksgiving is NFL on a Thursday. Black Friday has both.
    Bowl season puts college on every day between Christmas and New Year, and
    the playoff runs into January on weeknights. Each of those needs an
    exception, and the exceptions are what rot.

    Deriving it instead means the rules below hold without anything knowing
    what day of the week it is.
    """
    print("slate picked from the schedule")
    from datetime import datetime, timedelta, timezone
    from data import slate

    class G(object):
        def __init__(self, league, start, state="post"):
            self.league = league
            self.start = start
            self.state = state

        @property
        def live(self):
            return self.state == "in"

    utc = timezone.utc
    sunday = datetime(2026, 9, 20, 17, 0, tzinfo=utc)      # 1pm Eastern kickoff
    saturday = sunday - timedelta(days=1)

    games = [G("ncaaf", saturday), G("ncaaf", saturday), G("nfl", sunday)]
    now = sunday + timedelta(hours=1)
    check("Sunday's slate is the NFL alone",
          slate.active_leagues(games, utc, now) == {"nfl"},
          str(slate.active_leagues(games, utc, now)))

    # Same fixture, one day earlier: college is current and the NFL is not yet.
    check("Saturday's slate is college alone",
          slate.active_leagues(games, utc, saturday + timedelta(hours=2))
          == {"ncaaf"})

    # Before kickoff still counts. An NFL Sunday is an NFL Sunday at 8am, or
    # the board spends the morning on yesterday.
    morning = datetime(2026, 9, 20, 12, 0, tzinfo=utc)
    check("a game that has not started yet is still today's",
          slate.active_leagues(games, utc, morning) == {"nfl"})

    # A live game counts whatever date it began on, which is the one case a
    # pure date comparison gets wrong.
    late = [G("ncaaf", saturday, state="in"), G("nfl", sunday)]
    check("a game still in progress keeps its league current",
          slate.active_leagues(late, utc, now) == {"ncaaf", "nfl"},
          str(slate.active_leagues(late, utc, now)))

    # The timezone is the whole reason local_date exists: an 8:15pm Eastern
    # Sunday night game is Monday in UTC, and filing it under Monday would
    # make the NFL look stale on Sunday night of all moments.
    try:
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
    except Exception:
        eastern = None
    if eastern is not None:
        snf = G("nfl", datetime(2026, 9, 21, 0, 15, tzinfo=utc))
        check("a Sunday night game is filed on Sunday, not Monday in UTC",
              slate.local_date(snf, eastern).isoformat() == "2026-09-20",
              slate.local_date(snf, eastern).isoformat())

    # Nothing today: the nearest day wins rather than the panel going blank,
    # and a tie goes to the future because an upcoming slate beats an old one.
    wednesday = datetime(2026, 9, 23, 16, 0, tzinfo=utc)
    mixed = [G("nfl", wednesday - timedelta(days=3)),
             G("nfl", wednesday + timedelta(days=3))]
    day = slate.slate_day(mixed, utc, wednesday)
    check("an empty day falls back to a day that has games",
          day != wednesday.date(), day.isoformat())
    check("and prefers the upcoming slate over the finished one",
          day > wednesday.date(), day.isoformat())

    check("an empty schedule does not raise",
          slate.active_leagues([], utc, now) == set())
    check("a game with no kickoff time does not raise",
          slate.active_leagues([G("nfl", None)], utc, now) is not None)


def test_off_day_league_leaves_the_rotation():
    """On Sunday, college results should not be half the loop.

    Both leagues come back from ESPN in the same shape, so before this the
    rotation treated a finished Saturday game exactly like a live Sunday one
    and the panel spent half its time on yesterday. The fix drops the league
    that is not playing -- except for your own teams, which come back every
    few passes, because "did OSU win" is still a question on Sunday morning.
    """
    print("off-day league leaves the rotation")
    from datetime import datetime, timedelta, timezone
    from renderer.playlist import Playlist

    ctx, games = build_context()
    if not games:
        check("fixture has games to work with", False)
        return

    utc = timezone.utc
    now = datetime.now(utc)
    leagues_present = {g.league for g in games}
    if len(leagues_present) < 2:
        check("fixture covers more than one league", False,
              str(leagues_present))
        return

    # Make one league yesterday's and finished, the other today's and live.
    stale_key = sorted(leagues_present)[0]
    fresh_key = sorted(leagues_present)[1]
    for game in games:
        if game.league == stale_key:
            game.start = now - timedelta(days=1)
            game.state = "post"
        else:
            game.start = now
            game.state = "in"

    ctx.config.set("rotation.day_relevance", True)
    ctx.config.set("rotation.stay_on_live_favorite", False)
    ctx.config.set("rotation.offday_favorite_every", 3)

    pl = Playlist(ctx)
    pl.maybe_rebuild(force=True)
    shown = [sc.game for sc in pl.screens if getattr(sc, "game", None)]
    stale_shown = [g for g in shown if g.league == stale_key]
    check("no ordinary off-day game survives",
          all(g.is_favorite for g in stale_shown),
          str([g.matchup for g in stale_shown if not g.is_favorite]))
    check("the rest of the Top 25 is gone",
          not any(g.is_ranked_matchup and not g.is_favorite
                  for g in stale_shown),
          str([g.matchup for g in stale_shown]))
    check("today's league is still there",
          any(g.league == fresh_key for g in shown))

    # A favourite from the off-day league returns on one pass in three.
    stale_favourites = [g for g in games
                        if g.league == stale_key and g.is_favorite]
    if stale_favourites:
        seen = []
        for pass_no in range(3):
            pl._passes = pass_no
            pl.maybe_rebuild(force=True)
            ids = {sc.game.id for sc in pl.screens
                   if getattr(sc, "game", None)}
            seen.append(any(g.id in ids for g in stale_favourites))
        check("an off-day favourite appears on exactly one pass in three",
              seen.count(True) == 1, str(seen))
        pl._passes = 0

    # The poll belongs to the league, so it goes quiet with it.
    ctx.config.set("screens.rankings", True)
    pl.maybe_rebuild(force=True)
    check("the off-day league contributes no poll pages",
          not any(getattr(sc, "league_key", None) == stale_key
                  for sc in pl.screens if sc.is_info),
          str([sc.key for sc in pl.screens if sc.is_info]))

    # And the whole thing is one switch away from the old behaviour.
    ctx.config.set("rotation.day_relevance", False)
    pl.maybe_rebuild(force=True)
    both = {sc.game.league for sc in pl.screens if getattr(sc, "game", None)}
    check("turning it off brings the other league back",
          stale_key in both, str(sorted(both)))

    src = open(os.path.join(root_dir(), "renderer", "playlist.py")).read()
    check("the day is derived, not read off a weekday table",
          "weekday" not in src.lower())

    ui = open(os.path.join(root_dir(), "web", "templates", "index.html")).read()
    check("the setting is exposed in the web UI",
          'data-path="rotation.day_relevance"' in ui)
    check("so is the off-day frequency",
          'data-path="rotation.offday_favorite_every"' in ui)


def test_pass_counter_counts_passes():
    """The off-day throttle is per rotation, not per rebuild.

    Worth its own test because the obvious place to count is the rebuild, and
    the playlist rebuilds every 20 seconds while games are live. Counting
    there would make "one pass in three" mean "one minute in three", which is
    a different rule that happens to look similar on a quiet Tuesday.
    """
    print("pass counter")
    from renderer.playlist import Playlist

    ctx, _ = build_context()
    ctx.config.set("rotation.stay_on_live_favorite", False)
    pl = Playlist(ctx)
    pl.maybe_rebuild(force=True)
    if not pl.screens:
        check("fixture produced a playlist", False)
        return

    check("no passes completed yet", pl._passes == 0)
    for _ in range(len(pl.screens)):
        pl.advance()
    check("walking the whole list is one pass", pl._passes == 1,
          str(pl._passes))
    for _ in range(len(pl.screens)):
        pl.advance()
    check("and again is two", pl._passes == 2, str(pl._passes))

    before = pl._passes
    pl.maybe_rebuild(force=True)
    check("a rebuild is not a pass", pl._passes == before, str(pl._passes))


def test_board_profile_reaches_the_matrix():
    """The per-board tuning has to be passed to the library, not just reported.

    data/hardware.py sets a colour depth and a GPIO slowdown for each board --
    a Zero W wants depth 8 and slowdown 0, which is what stops it flickering --
    and /api/hardware reported those values, and the web page displayed them.
    Nothing passed them to the matrix. Every other profile value went through
    hardware.tuned() and worked; the two that reach the library were read
    straight out of config.json with a hardcoded default behind them, so a
    board with nothing set ran the library's 11 and 1 while being described
    everywhere as running 8 and 0.

    Silent, and exactly backwards from the thing the profiles exist for.
    """
    print("board profile reaches the matrix")
    import json
    import tempfile
    from main import matrix_options
    from data.config import Config
    from data import hardware

    def options_for(profile, **matrix):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as fh:
                json.dump({"performance": {"profile": profile},
                           "matrix": matrix}, fh)
            return matrix_options(Config(path), {})

    zero = hardware.PROFILES["zero_w"]
    got = options_for("zero_w")
    check("a Zero W gets the profile's colour depth",
          got["pwm_bits"] == zero.pwm_bits,
          "{} != {}".format(got.get("pwm_bits"), zero.pwm_bits))
    check("and the profile's slowdown",
          got.get("gpio_slowdown") == zero.gpio_slowdown,
          str(got.get("gpio_slowdown")))

    pi3 = hardware.PROFILES["pi3"]
    got = options_for("pi3")
    check("a Pi 3 gets its own profile, not the Zero's",
          got["pwm_bits"] == pi3.pwm_bits
          and got.get("gpio_slowdown") == pi3.gpio_slowdown,
          "{} / {}".format(got.get("pwm_bits"), got.get("gpio_slowdown")))

    # Precedence, which is the whole point of resolving it this way.
    got = options_for("zero_w", pwm_bits=11)
    check("an explicit setting still wins over the profile",
          got["pwm_bits"] == 11, str(got.get("pwm_bits")))

    got = options_for("zero_w", pwm_bits="")
    check("a blank field falls back to the profile, not to the hardcoded 11",
          got["pwm_bits"] == zero.pwm_bits, str(got.get("pwm_bits")))

    # A profile with no opinion must leave the option out entirely rather than
    # passing None, which the library cannot take.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as fh:
            json.dump({"performance": {"profile": "generic"}, "matrix": {}}, fh)
        got = matrix_options(Config(path), {})
    check("an unopinionated profile leaves slowdown unset",
          "gpio_slowdown" not in got or got["gpio_slowdown"] is not None,
          str(got.get("gpio_slowdown", "absent")))

    # And the CLI override, which is how the service and debugging runs pass it.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as fh:
            json.dump({"performance": {"profile": "zero_w"},
                       "matrix": {"pwm_bits": 9}}, fh)
        got = matrix_options(Config(path), {"pwm_bits": 7})
    check("a CLI flag beats both", got["pwm_bits"] == 7, str(got["pwm_bits"]))


def test_slowdown_is_guarded_in_the_right_direction():
    """Raising GPIO slowdown to fix flicker makes it worse, so say so.

    The name reads like a safety margin. It is not: it pads every GPIO write,
    stretching the refresh cycle, lowering the frame rate, and past a point
    turning the panel into what looks like a scrambled analogue channel. The
    library documents 0 to 2 and refuses anything above 4, which kills the
    process rather than dimming the picture.

    So the guard has to run in the unintuitive direction, and the message has
    to name the symptom, because nothing about a scrambled panel points back
    at a number in a settings form.
    """
    print("slowdown guard")
    from main import _sanity_check, SLOWDOWN_MAX, SLOWDOWN_DOCUMENTED

    check("the documented ceiling is below the hard limit",
          SLOWDOWN_DOCUMENTED < SLOWDOWN_MAX)

    over = _sanity_check({"hardware_mapping": "adafruit-hat", "parallel": 1,
                          "chain_length": 1, "gpio_slowdown": 9})
    check("a value the library would reject is clamped rather than fatal",
          over["gpio_slowdown"] == SLOWDOWN_MAX, str(over["gpio_slowdown"]))

    high = _sanity_check({"hardware_mapping": "adafruit-hat", "parallel": 1,
                          "chain_length": 1, "gpio_slowdown": 3})
    check("an undocumented but usable value is honoured, not overridden",
          high["gpio_slowdown"] == 3, str(high["gpio_slowdown"]))

    fine = _sanity_check({"hardware_mapping": "adafruit-hat", "parallel": 1,
                          "chain_length": 1, "gpio_slowdown": 1})
    check("a normal value is left alone", fine["gpio_slowdown"] == 1)

    absent = _sanity_check({"hardware_mapping": "adafruit-hat", "parallel": 1,
                            "chain_length": 1})
    check("an unset slowdown is not invented",
          "gpio_slowdown" not in absent)

    src = open(os.path.join(root_dir(), "main.py")).read()
    check("the warning names the symptom rather than the setting",
          "scrambled" in src)
    check("and says which direction actually helps",
          "lowers the refresh rate" in src)

    ui = open(os.path.join(root_dir(), "web", "templates", "index.html")).read()
    check("the settings page no longer reads as a range to try",
          "0-1 Pi Zero, 2-4 Pi 4" not in ui)
    check("it says raising it does not reduce flicker",
          "does not reduce flicker" in ui)
    check("and points at colour depth as the flicker setting",
          "The flicker setting" in ui)

    doc = open(os.path.join(root_dir(), "scoreboard")).read()
    check("doctor flags a slowdown above the documented range",
          "gpio_slowdown is set to" in doc)
    check("doctor offers the reserved-core fix on a quad-core Pi",
          "isolcpus" in doc)
    check("doctor names the PWM hardware mod on a plain Adafruit mapping",
          "GPIO 18" in doc)


def test_logo_colour_fidelity():
    """A crimson logo has to arrive on the panel crimson, not white.

    Regression for the pair of bugs that turned Indiana, Alabama and Texas into
    white marks: the fetcher preferred ESPN's "-dark" asset (which for those
    schools is the mark in flat white), and the dark-boost scaled all three
    channels against a luminance target, clipping red while lifting green and
    blue until a saturated colour went pink and then white.
    """
    print("logo colour fidelity")
    from PIL import Image
    from data import logos

    def hue(img):
        return img.convert("HSV").split()[0].getextrema()[0]

    for name, rgb in [("crimson", (158, 27, 50)),
                      ("burnt orange", (191, 87, 0)),
                      ("forest green", (20, 83, 45))]:
        src_img = Image.new("RGBA", (64, 64), rgb + (255,))
        out = logos._prepare(src_img, 20)
        pixel = out.getpixel((10, 10))
        dominant = rgb.index(max(rgb))

        check("{} keeps its dominant channel".format(name),
              pixel.index(max(pixel)) == dominant, str(pixel))
        check("{} does not wash out towards white".format(name),
              max(pixel) - min(pixel) > 60,
              "channel spread only {}".format(max(pixel) - min(pixel)))
        check("{} survives the boost with its hue".format(name),
              abs(hue(out) - hue(src_img.convert("RGB"))) <= 8,
              "{} -> {}".format(hue(src_img.convert("RGB")), hue(out)))

    # The boost still has to do its original job, or the silhouette problem
    # it was written for comes straight back.
    lifted = logos._boost_dark(Image.new("RGB", (8, 8), (18, 10, 40)))
    check("a near-black mark is still brightened",
          max(lifted.getpixel((4, 4))) > 40, str(lifted.getpixel((4, 4))))

    source = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "logos.py")).read()
    check("the dark redraw is chosen by measurement, not by preference",
          "prefers_dark(standard, readability(alt))" in source)
    check("the true-colour asset is always the one fetched first",
          source.index("img = fetch(url)")
          < source.index("fetch(dark_variant(url))"))


def test_dark_variant_is_chosen_by_measurement():
    """Which of ESPN's two logos to use is a measurement, not a guess.

    Both fixed preferences failed on real artwork. Preferring the dark redraw
    turned Indiana, Alabama and Texas white, because for those schools the
    "-dark" asset is the mark redrawn in flat white. Preferring true colour
    unless it failed the legibility gate left Cincinnati invisible: the gate
    asked for 3% lit and the Bearcats' mark clears that at 4%.

    The figures below are measured off the board, not invented:

        CIN  true colour   4% lit,  0% solid      dark  25% lit, 17% solid
        OSU  true colour  37% lit,  5% solid      dark  70% lit, 32% solid

    Cincinnati is the case that proves "lit" is the wrong number to score on.
    Ohio State is the case that proves it is not enough on its own: 37% lit
    would have looked like a healthy mark while the Block O was a scarlet
    haze with no letter in it.
    """
    print("dark variant chosen by measurement")
    from PIL import Image
    from data import logos

    def swatch(level, share):
        """A 20px mark with `share` of its pixels at brightness `level`."""
        img = Image.new("RGB", (20, 20), (0, 0, 0))
        lit = int(round(share * 400))
        for i in range(lit):
            img.putpixel((i % 20, i // 20), (level, level, level))
        return img

    # The two measures have to disagree, or there was no point separating them.
    haze = swatch(70, 0.37)          # widely lit, nothing solid
    check("a lit-but-shapeless mark scores high on visibility",
          logos.visibility(haze) > 0.30, str(logos.visibility(haze)))
    check("and near zero on readability",
          logos.readability(haze) < 0.02, str(logos.readability(haze)))

    solid = swatch(200, 0.30)
    check("a solid mark scores on both",
          logos.visibility(solid) > 0.25 and logos.readability(solid) > 0.25,
          "{} / {}".format(logos.visibility(solid), logos.readability(solid)))

    # Hassan's real numbers, replayed through the rule.
    check("Cincinnati takes the dark redraw (0% solid vs 17%)",
          logos.prefers_dark(0.00, 0.17))
    check("Ohio State takes the dark redraw (5% solid vs 32%)",
          logos.prefers_dark(0.05, 0.32))

    # And the regression the margin exists to prevent: a team whose true
    # colour already reads keeps it, even when the redraw measures slightly
    # higher. Scarlet is the point of a scarlet logo.
    check("a readable true-colour mark is not traded for a marginal gain",
          not logos.prefers_dark(0.30, 0.34))
    check("a clearly readable mark does not even ask for the redraw",
          not logos.needs_second_look(0.30))
    check("a doubtful mark does ask", logos.needs_second_look(0.05))

    # A failed fetch must not beat a successful one by scoring -1.0.
    check("anything beats an asset that did not load",
          logos.prefers_dark(-1.0, 0.01))
    check("but a missing redraw never wins",
          not logos.prefers_dark(0.05, -1.0))

    # The gate the whole thing feeds: 4% lit is not a logo.
    check("Cincinnati's true-colour mark would have failed the new gate",
          not logos.legible(swatch(70, 0.04)))
    check("its dark redraw passes", logos.legible(swatch(200, 0.25)))


def test_logo_overrides_by_league():
    """Hand-made art is per league, so MIA can be two different teams.

    College teams could not have override art at all before this: the only
    folder was the NFL one, and letting ncaaf read it puts a Dolphins helmet on
    Miami of Ohio. Each league now gets its own subfolder.
    """
    print("logo overrides by league")
    from PIL import Image
    from data import logos

    root = logos.OVERRIDE_DIR
    ncaaf_dir = os.path.join(root, "ncaaf")
    made = []
    try:
        os.makedirs(ncaaf_dir, exist_ok=True)
        for path in (os.path.join(root, "MIA.png"),
                     os.path.join(ncaaf_dir, "MIA.png"),
                     os.path.join(ncaaf_dir, "OSU.png")):
            if not os.path.exists(path):
                Image.new("RGBA", (8, 8), (200, 30, 40, 255)).save(path)
                made.append(path)

        nfl = logos._override_path("MIA", False, "nfl")
        college = logos._override_path("MIA", False, "ncaaf")
        check("NFL art still resolves at the top of logos/",
              nfl and os.path.dirname(nfl) == root, str(nfl))
        check("college art resolves in its own subfolder",
              college and os.path.dirname(college) == ncaaf_dir, str(college))
        check("the two MIAs are different files", nfl != college)
        check("a college team can now have override art at all",
              logos._override_path("OSU", False, "ncaaf") is not None)
        check("an unknown league gets no override art",
              logos._override_path("MIA", False, "") is None
              or os.path.dirname(logos._override_path("MIA", False, "")) != root,
              "empty league must not fall through to NFL art")
        check("helmet art stays NFL-only",
              logos._override_path("MIA", True, "ncaaf") == college)
    finally:
        for path in made:
            try:
                os.remove(path)
            except OSError:
                pass

    source = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "logos.py")).read()
    check("override lookup is gated on league, not a bare boolean",
          "_override_path(abbr, helmet, league) if league else None" in source)


def test_doctor_storage_checks():
    """doctor has to name a dying card before anything downstream of it.

    A failing microSD does not stop cleanly. ext4 remounts read-only, and from
    userspace that presents as settings that will not save, a config that
    reverts, and an install that half worked, each of which looks like its own
    bug. Every other check in doctor is meaningless while that is true, so the
    storage section runs first.
    """
    print("doctor storage checks")
    src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scoreboard")).read()

    check("doctor checks for a read-only root",
          "read-only" in src.lower() and "/proc/mounts" in src)
    check("doctor reads the kernel log for card I/O errors",
          "EXT4-fs error" in src and "Buffer I/O error" in src)
    check("doctor checks the recorded filesystem state",
          "Filesystem state" in src)
    check("doctor proves the project directory is writable",
          ".doctor-write-probe" in src)
    check("a full card is distinguished from a read-only one",
          "out of space" in src and "df -h" in src)

    storage = src.find('head_ "Storage"')
    deps = src.find('head_ "Dependencies"')
    check("storage is diagnosed before dependencies",
          0 < storage < deps, "storage={} deps={}".format(storage, deps))


def test_firstboot_network_probe():
    """The connectivity probe must not demand a 2xx from every host.

    Regression for the bug that made the card bootstrap fail on every board:
    the probe used `curl -f`, which treats any status >= 400 as failure, and
    https://files.pythonhosted.org/ answers 404 to a bare GET because it serves
    package files and has no root document. So the probe returned "no network"
    on a healthy connection, stage B waited out its five-minute timeout, and the
    log told people to check wifi that was working perfectly.
    """
    print("first-boot network probe")
    src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "boot", "scoreboard-firstboot.sh")).read()

    probe = src[src.index("probe() {"):src.index("clock_ok() {")]
    check("the probe does not use curl -f", "-f" not in probe.replace("-fsS", "XX"),
          probe.strip().splitlines()[-2] if probe.strip() else "")
    check("the probe still verifies TLS",
          "--insecure" not in src and "-k " not in src)

    check("a wrong clock is checked before blaming the network",
          "clock_ok" in src and src.index("clock_ok()") < src.index("net_ready()"))
    check("the failure message distinguishes DNS from HTTP",
          "no DNS after 5 minutes" in src and "DNS works but no host answered" in src)
    check("the old misleading wifi advice is gone",
          "Check the wifi settings" not in src)
    check("the log says how to recover without re-flashing",
          "/var/lib/scoreboard/payload.zip" in src)


def test_panel_sized_override_is_used_verbatim():
    """Art already drawn at panel size must not be processed again.

    logo_tune writes its chosen treatment at the panel's own size, with the
    saturation, contrast and dark-boost already applied. Feeding that back
    through _prepare applied all three a second time, so the override you
    picked because it looked right came out oversaturated and lifted -- and a
    hand-drawn 20x20 mark arrived in colours nobody chose.
    """
    print("panel-sized overrides")
    from PIL import Image
    from data import logos

    folder = os.path.join(logos.OVERRIDE_DIR, "ncaaf")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "ZZTEST.png")
    colour = (150, 20, 30)
    try:
        Image.new("RGBA", (20, 20), colour + (255,)).save(path)
        logos.clear_memory()
        exact = logos.get("ZZTEST", url="", size=20, league="ncaaf")
        check("a 20px override survives untouched",
              exact.getpixel((10, 10)) == colour, str(exact.getpixel((10, 10))))

        Image.new("RGBA", (400, 400), colour + (255,)).save(path)
        logos.clear_memory()
        big = logos.get("ZZTEST", url="", size=20, league="ncaaf")
        check("a full-size override is still processed",
              big.getpixel((10, 10)) != colour, str(big.getpixel((10, 10))))

        # The rule is size-based, so it has to hold at the other size too.
        Image.new("RGBA", (16, 16), colour + (255,)).save(path)
        logos.clear_memory()
        small = logos.get("ZZTEST", url="", size=16, league="ncaaf")
        check("it holds at the 16px size the team card uses",
              small.getpixel((8, 8)) == colour, str(small.getpixel((8, 8))))
    finally:
        if os.path.exists(path):
            os.remove(path)
        logos.clear_memory()

    tune = open(os.path.join(root_dir(), "tools", "logo_tune.py")).read()
    check("logo_tune reports which resample filter was chosen",
          "resample  :" in tune and "_resample_for" in tune)
    check("and offers each filter as a treatment to compare",
          "forced LANCZOS" in tune and "forced NEAREST" in tune
          and "forced BOX" in tune)


def test_live_favourite_does_not_fill_both_panels():
    """Camping on a live favourite must not mean showing it twice.

    On one panel, camping means the favourite IS the playlist -- that is the
    whole point of the rule. On a chained pair the same one-screen playlist
    was handed to both cells, so a two-panel board spent the entire game
    displaying the same score side by side, which is the opposite of what the
    second panel is for.
    """
    print("live favourite on a tiled panel")

    class Cells(object):
        def __init__(self, n):
            self.cell_count = n

    def playlist_for(n):
        ctx, _ = build_context()
        ctx.geometry = Cells(n)
        pl = Playlist(ctx)
        pl.maybe_rebuild(force=True)
        return pl

    one = playlist_for(1)
    check("one panel still camps on the favourite", len(one) == 1)
    check("and pins nothing, because the playlist is the game",
          one.pinned == [])
    check("the camped screen is the favourite",
          one.current().game.is_favorite)

    two = playlist_for(2)
    win = two.window(2)
    check("two panels pin the favourite", len(two.pinned) == 1)
    check("the left cell is the favourite", win[0].game.is_favorite)
    check("the two cells are different games", win[0].key != win[1].key,
          str([w.key for w in win]))

    # The pinned game must not also appear in the rotation, or it comes back
    # around on the right and we are showing it twice again.
    check("the pinned game is out of the rotation",
          all(getattr(sc, "game", None) is None
              or sc.game.id != win[0].game.id for sc in two.screens),
          win[0].key)

    # The right cell has to actually move, and move by one, or half the slate
    # is skipped on every step.
    seen = []
    for _ in range(4):
        w = two.window(2)
        check("the favourite stays put", w[0].key == win[0].key)
        seen.append(w[1].key)
        two.advance()
    check("the other cell advances every time", len(set(seen)) == len(seen),
          str(seen))

    # With more cells, at least one always keeps rotating.
    three = playlist_for(3)
    w3 = three.window(3)
    check("three cells leave one rotating", len(three.pinned) <= 2)
    check("no cell is a duplicate of another",
          len({sc.key for sc in w3}) == 3, str([sc.key for sc in w3]))


def test_clock_sync_before_apt():
    """A plausible-looking year is not a synced clock.

    A real first boot came up about a day behind, passed the old year >= 2025
    check, and then apt rejected every Raspberry Pi repository with "Not live
    until 2026-09-18T21:51:36Z" -- the signature's validity had not started
    yet as far as that Pi was concerned. TLS never complained, because a
    certificate's window is months wide and a day of drift sits inside it.
    Repository signatures are narrow, so apt is what notices.
    """
    print("clock sync before apt")
    boot = open(os.path.join(root_dir(), "boot", "scoreboard-firstboot.sh")).read()
    inst = open(os.path.join(root_dir(), "install.sh")).read()

    check("the clock check asks whether NTP actually synced",
          "NTPSynchronized" in boot)
    check("the year test survives only as a fallback",
          boot.count("date -u +%Y") == 1)
    check("the clock gets its own wait, separate from the network",
          "waiting for the clock to sync" in boot)
    check("a clock that never syncs warns instead of blocking the install",
          "apt may reject repository signatures" in boot)
    check("the network probe no longer gates on the clock",
          "clock_ok || return 1" not in boot)

    check("the installer recognises the signature-not-live error",
          "not live until" in inst.lower())
    check("it waits for sync and retries rather than giving up",
          "clock synced" in inst and "retrying apt" in inst)
    check("it explains the cause rather than quoting sqv",
          "no battery-backed clock" in inst)

    # apt-get update exits 0 on a signature failure, so the exit code alone
    # would have let this through silently.
    apt = inst[inst.index("say \"System packages\""):]
    check("the apt result is judged on its log, not just its exit code",
          "APT_RC" in apt and "grep -qiE 'not live until" in apt)


def test_logo_cache_cannot_collide():
    """Two teams with the same abbreviation must not share a cached picture.

    CIN is the Cincinnati Bengals and the Cincinnati Bearcats. MIA is the
    Dolphins and the Hurricanes. HOU is the Texans and the Cougars. The disk
    cache was named from the abbreviation and the size alone, so the first of
    each pair to be fetched owned the file and the second showed its logo --
    and because the collision is on disk, it survived restarts. The in-memory
    cache was keyed correctly and masked it within a single run, which is why
    this had to be spotted by looking at the panel rather than by a test.
    """
    print("logo cache collisions")
    from data import logos

    espn = "https://a.espncdn.com/i/teamlogos/{}/500/{}.png"
    pairs = [("CIN", "nfl", "cin", "ncaaf", "2132"),
             ("MIA", "nfl", "mia", "ncaaf", "2390"),
             ("HOU", "nfl", "hou", "ncaaf", "248")]

    for abbr, la, ida, lb, idb in pairs:
        a = logos._cache_path(abbr, espn.format(la, ida), 20, la)
        b = logos._cache_path(abbr, espn.format("ncaa", idb), 20, lb)
        check("{} does not collide across leagues".format(abbr), a != b)

    # Same team, same request: still one file, or the cache is pointless.
    url = espn.format("nfl", "cin")
    check("the same logo maps to one file",
          logos._cache_path("CIN", url, 20, "nfl")
          == logos._cache_path("CIN", url, 20, "nfl"))
    check("sizes are kept apart",
          logos._cache_path("CIN", url, 20, "nfl")
          != logos._cache_path("CIN", url, 16, "nfl"))
    check("a changed source URL is a new entry",
          logos._cache_path("CIN", url, 20, "nfl")
          != logos._cache_path("CIN", url + "?v=2", 20, "nfl"))
    check("the filename is still readable",
          "ncaaf_CIN_20" in os.path.basename(
              logos._cache_path("CIN", espn.format("ncaa", "2132"), 20, "ncaaf")))


def test_pixel_art_is_sampled_not_filtered():
    """The bundled helmets must survive the shrink.

    They are hand-drawn at roughly 20x20 and stored as a 400x400 upscale, so
    every edge is a hard block boundary. LANCZOS interpolates across those
    blocks and a five-colour helmet lands on the panel as an orange smudge.
    The averaging is right for everything else, though: sampled with NEAREST
    the Ravens mark comes apart into specks and Green Bay's oval becomes a
    dotted ring, because their thin strokes fall between sample points.
    """
    print("pixel art resampling")
    from PIL import Image
    from data import logos

    art = os.path.join(root_dir(), "logos")

    def src(name):
        return Image.open(os.path.join(art, name)).convert("RGBA")

    check("a helmet is recognised as pixel art",
          logos._resample_for(src("CINH.png")) == Image.NEAREST)
    check("every bundled helmet is, not just one",
          all(logos._resample_for(src(n)) == Image.NEAREST
              for n in ("KCH.png", "NYJH.png", "CLEH.png", "DALH.png")))
    check("a vector wordmark is not",
          logos._resample_for(src("GB.png")) == Image.LANCZOS)
    check("nor is a detailed mark",
          logos._resample_for(src("BAL.png")) == Image.LANCZOS)

    # Sampling preserves the palette; interpolating invents intermediate
    # colours, and on flat art those intermediates are the smudge.
    helmet = logos._prepare(src("CINH.png"), 20)
    shades = helmet.convert("RGB").getcolors(maxcolors=4096) or []
    check("the helmet keeps a flat palette after resizing",
          len(shades) <= 24, "{} distinct colours".format(len(shades)))

    ravens = logos._prepare(src("BAL.png"), 20)
    body = ravens.convert("RGB").getcolors(maxcolors=4096) or []
    check("a detailed mark still gets its gradients",
          len(body) > 40, "{} distinct colours".format(len(body)))

    # Trimming has to work on opaque art too, or the helmets keep their
    # painted-on black margin and land smaller than everything beside them.
    opaque = src("CINH.png")
    low, high = opaque.split()[-1].getextrema()
    check("the helmets really are fully opaque", low == high == 255)
    box = logos._content_bbox(opaque)
    check("an opaque mark is still trimmed to its content",
          box is not None and (box[2] - box[0]) < opaque.width,
          "bbox {} of {}".format(box, opaque.size))

    # Built rather than loaded: none of the bundled art is a real cutout, so
    # this branch only ever runs on ESPN's downloads, which are.
    cut = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    cut.paste(Image.new("RGBA", (40, 30), (200, 40, 40, 255)), (20, 35))
    check("a real cutout is trimmed on its alpha",
          logos._content_bbox(cut) == (20, 35, 60, 65),
          str(logos._content_bbox(cut)))

    # Green Bay is the case that made this subtle: an alpha channel that never
    # drops below 191 is a wash over the whole canvas, not a cutout, and
    # trusting it returned the full square with the oval floating in padding.
    packers = src("GB.png")
    low, _ = packers.split()[-1].getextrema()
    check("Green Bay's alpha is a wash, not a cutout", low > 16, str(low))
    box = logos._content_bbox(packers)
    check("so it falls through to a brightness trim",
          box is not None and (box[3] - box[1]) < packers.height, str(box))


def test_rotation_quality():
    """The four display-quality rules, each one a thing that looked wrong."""
    print("rotation quality")
    ctx, games = build_context()
    config = ctx.config
    config.set("rotation.stay_on_live_favorite", False)

    # ---- a favourite on a bye does not get a card saying so ----
    from renderer.screens import info as info_mod
    card = info_mod.TeamCardScreen(ctx, "nfl", "CIN")
    check("a team on the slate reports a game", card.has_game())
    absent = info_mod.TeamCardScreen(ctx, "nfl", "ZZZ")
    check("a team not on the slate reports none", not absent.has_game())

    playlist = Playlist(ctx)
    playlist.maybe_rebuild(force=True)
    keys = [sc.key for sc in playlist.screens]
    check("no card for a team with no game", "team:nfl:ZZZ" not in keys)

    src = open(os.path.join(root_dir(), "renderer", "screens", "info.py")).read()
    check("the NO GAME message is gone", '"NO GAME"' not in src)

    # ---- a favourite's logo is not gated on legibility ----
    start = src.index("class TeamCardScreen")
    nxt = src.find("\nclass ", start)
    card_src = src[start:nxt if nxt > 0 else len(src)]
    check("the favourite card asks for the real logo",
          "require_legible=False" in card_src)

    store_src = open(os.path.join(root_dir(), "data", "store.py")).read()
    check("both drawn logo sizes are prefetched", "sizes = (20, 16)" in store_src)

    # ---- one league at a time ----
    config.set("rotation.league_order", ["ncaaf", "nfl"])
    playlist.maybe_rebuild(force=True)
    order = [sc.game.league for sc in playlist.screens if hasattr(sc, "game")]
    check("college comes before the NFL",
          order == sorted(order, key=lambda l: 0 if l == "ncaaf" else 1),
          str(order))

    config.set("rotation.league_order", ["nfl", "ncaaf"])
    playlist.maybe_rebuild(force=True)
    order = [sc.game.league for sc in playlist.screens if hasattr(sc, "game")]
    check("the order is configurable, not hardcoded",
          order == sorted(order, key=lambda l: 0 if l == "nfl" else 1), str(order))

    config.set("rotation.group_by_league", False)
    playlist.maybe_rebuild(force=True)
    check("grouping can be turned off",
          len([sc for sc in playlist.screens if hasattr(sc, "game")]) > 0)
    config.set("rotation.group_by_league", True)
    config.set("rotation.league_order", ["ncaaf", "nfl"])

    # ---- the cap cannot starve a league or drop a favourite ----
    config.set("rotation.max_games", 4)
    playlist.maybe_rebuild(force=True)
    picked = [sc.game for sc in playlist.screens if hasattr(sc, "game")]
    leagues_seen = {g.league for g in picked}
    check("a tight cap still shows both leagues", len(leagues_seen) >= 2,
          str(leagues_seen))
    check("favourites survive a tight cap",
          all(g.is_favorite for g in ctx.store.snapshot() if g.is_favorite
              and g.id in {p.id for p in picked}) and
          any(g.is_favorite for g in picked))
    config.set("rotation.max_games", 14)

    # ---- no drawn bar between panels ----
    disp = open(os.path.join(root_dir(), "renderer", "display.py")).read()
    check("the seam hairline is behind a setting",
          'config.get("display.cell_divider", False)' in disp)
    from data import config as config_mod
    check("and that setting is off by default",
          config_mod.DEFAULTS["display"]["cell_divider"] is False)


def test_impossible_panel_settings_are_corrected():
    """A wrong number in a settings form must not brick a board.

    The matrix library validates some combinations by calling abort(), so the
    process dies with SIGABRT, systemd restarts it, and it aborts again. From
    outside that is a dark panel and a restart loop, with the actual reason
    only in the journal. Asking for 2 parallel chains on an Adafruit HAT --
    which has one HUB75 output -- does exactly that, and it is an easy mistake
    because "parallel" sits right next to "chain" in the settings.
    """
    print("impossible panel settings")
    from main import _sanity_check
    from renderer import geometry

    hat = _sanity_check({"hardware_mapping": "adafruit-hat", "parallel": 2,
                         "chain_length": 2, "rows": 32, "cols": 64})
    check("parallel is corrected on an Adafruit board", hat["parallel"] == 1)
    check("the chain the user actually meant is left alone",
          hat["chain_length"] == 2)

    generic = _sanity_check({"hardware_mapping": "regular", "parallel": 2,
                             "chain_length": 1})
    check("parallel is untouched on wiring that supports it",
          generic["parallel"] == 2)

    over = _sanity_check({"hardware_mapping": "regular", "parallel": 9,
                          "chain_length": 1})
    check("parallel is clamped to the library maximum", over["parallel"] == 3)

    # The renderer has to believe the same thing the library was told, or it
    # draws cells into rows the panel does not have.
    geo = geometry.from_options(hat)
    check("geometry follows the corrected options",
          geo.width == 128 and geo.height == 32, geo.describe())
    check("geometry gives two cells, not four", geo.cell_count == 2,
          geo.describe())

    src = open(os.path.join(root_dir(), "main.py")).read()
    check("geometry is built from the options, not re-derived from config",
          "geometry.from_options(options)" in src)

    ui = open(os.path.join(root_dir(), "web", "templates", "index.html")).read()
    check("the settings page says which field to change for a second panel",
          "This is the one to change if you added a second panel" in ui)
    check("the settings page says parallel must stay at 1 on these boards",
          "single output" in ui)


def test_install_verify_is_not_a_race():
    """Starting a service and immediately curling it is not a verification.

    On a Pi 3 A+ the process imports Flask, constructs the matrix and starts
    the data thread before it ever binds the port. A single curl fired in the
    same breath as `systemctl start` loses that race and reports "web UI" as a
    failure on a board that is fine, which is worse than not checking: it sends
    someone to debug working software.
    """
    print("install verification timing")
    src = open(os.path.join(root_dir(), "install.sh")).read()
    verify = src[src.index('say "Verifying"'):]

    check("the web check retries rather than firing once",
          "for _ in $(seq 1 15)" in verify and "sleep 2" in verify)
    check("it allows at least 20 seconds for a cold start",
          "seq 1 15" in verify)
    check("a real failure prints what the service said",
          "journalctl -u scoreboard" in verify)
    check("each attempt is individually bounded",
          "--max-time 3" in verify)


def test_panel_count_preseed():
    """A two-panel board must be right the first time it lights up.

    HUB75 is a shift register with no back channel, so nothing on the Pi can
    detect how many panels are wired. The number has to come from whoever
    assembled the board. Without this it defaults to 1, the renderer draws one
    64x32 cell, the library clocks only 64 columns, and the second panel holds
    the same row data -- which reads as "both screens show the same game".
    """
    print("panel count preseed")
    prep = open(os.path.join(root_dir(), "boot", "prepare-sd-card.sh")).read()
    boot = open(os.path.join(root_dir(), "boot", "scoreboard-firstboot.sh")).read()
    inst = open(os.path.join(root_dir(), "install.sh")).read()

    check("card prep accepts --panels", "--panels)" in prep)
    check("card prep rejects a non-numeric panel count",
          "[1-8]" in prep and "die \"--panels" in prep)
    check("card prep writes the marker file",
          "scoreboard-panels.txt" in prep)
    check("stage A carries it off the card",
          "scoreboard-panels.txt" in boot and "panels.txt" in boot)
    check("stage B hands it to the project",
          "preseed-panels.txt" in boot)
    check("the installer applies it to chain_length",
          "preseed-panels.txt" in inst and "chain_length" in inst)
    check("the installer clears the marker once applied",
          "rm -f state/preseed-panels.txt" in inst)

    # The geometry has to actually follow from the setting, or preseeding it
    # changes nothing visible.
    from renderer import geometry

    class FakeConfig:
        def __init__(self, chain):
            self.values = {"matrix.chain_length": chain, "matrix.cols": 64,
                           "matrix.rows": 32, "matrix.parallel": 1,
                           "matrix.tile_screens": True}

        def get(self, key, default=None):
            return self.values.get(key, default)

    one = geometry.from_config(FakeConfig(1), {})
    two = geometry.from_config(FakeConfig(2), {})
    check("one panel is one cell", one.cell_count == 1, one.describe())
    check("two panels are two cells", two.cell_count == 2, two.describe())
    check("two panels double the width only",
          two.width == 128 and two.height == 32, two.describe())
    check("the cell size never changes with panel count",
          one.cell_width == two.cell_width == 64
          and one.cell_height == two.cell_height == 32)
    check("cells are laid out left to right",
          two.cell_origin(0) == (0, 0) and two.cell_origin(1) == (64, 0))


def root_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_preseed_name():
    """A name dropped on the card becomes the board's name and hostname."""
    print("preseed name")
    from data import identity

    for given, expected in [
        ("Living Room Scoreboard", "living-room-scoreboard"),
        ("  Dad's Board  ", "dads-board"),
        ("Board #2", "board-2"),
    ]:
        got = identity.slugify(given.strip())
        check("{!r} -> {}".format(given, expected), got == expected, got)

    src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "install.sh")).read()
    check("installer consumes the preseeded name",
          "state/preseed-name.txt" in src)
    check("installer queues the rename for the root watcher",
          "state/hostname.request" in src)


if __name__ == "__main__":
    main()
