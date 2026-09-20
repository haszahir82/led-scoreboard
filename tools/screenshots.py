"""Render every screen against fixture data and write a contact sheet.

    python3 -m tools.screenshots            # -> out/contact_sheet.png
    python3 -m tools.screenshots --scale 6

This is the layout feedback loop: it needs no Pi, no network, and no season in
progress, so a change to a layout can be checked in seconds.
"""

import argparse
import os

from PIL import Image, ImageDraw, ImageFont

from data import espn, leagues
from data.config import Config
from data.models import RankingEntry, StandingEntry
from renderer import layout
from renderer.display import RenderContext
from renderer.screens import game as game_screens
from renderer.screens import info as info_screens
from renderer.layout import Frame
from tools import fixtures

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "out")


class FakeStore:
    """Store stand-in backed by fixtures instead of HTTP."""

    def __init__(self, games, rankings_map, standings_map, weather):
        self.games = games
        self.rankings = rankings_map
        self.standings = standings_map
        self.weather = weather
        self.generation = 1
        self.online = True
        self.error = ""

    def snapshot(self):
        return list(self.games)

    def get_rankings(self, key):
        return self.rankings.get(key, [])

    def get_standings(self, key):
        return self.standings.get(key, [])

    def get_weather(self):
        return self.weather

    def live_favorite(self):
        return next((g for g in self.games if g.is_favorite and g.live), None)

    def live_favorites(self):
        return [g for g in self.games if g.is_favorite and g.live]

    def any_live(self):
        return any(g.live for g in self.games)

    def stale_seconds(self):
        return 0


def build_context():
    config = Config(path=os.path.join(OUT_DIR, "_screenshot_config.json"))
    config.set("leagues.ncaaf.favorites", ["OSU"])
    config.set("leagues.nfl.favorites", ["CIN"])

    games = []
    for payload, key in ((fixtures.ncaaf_scoreboard(), "ncaaf"),
                         (fixtures.nfl_scoreboard(), "nfl")):
        league = leagues.get(key)
        for event in payload["events"]:
            games.append(espn.parse_event(event, league))

    favourites = {"ncaaf": ["OSU"], "nfl": ["CIN"]}
    for game in games:
        game.is_favorite = game.involves(favourites.get(game.league, []))

    rankings_map = {"ncaaf": [
        RankingEntry(rank=r["current"], abbr=r["team"]["abbreviation"],
                     name=r["team"]["nickname"], record=r["recordSummary"],
                     points=r["points"], trend=r["trend"],
                     logo_url=r["team"]["logos"][0]["href"])
        for r in fixtures.rankings()["rankings"][0]["ranks"]]}

    standings_map = {"nfl": []}
    for division in fixtures.nfl_standings()["children"]:
        for entry in division["standings"]["entries"]:
            stats = {s["name"]: s["displayValue"] for s in entry["stats"]}
            standings_map["nfl"].append(StandingEntry(
                abbr=entry["team"]["abbreviation"],
                name=entry["team"]["displayName"],
                wins=int(stats["wins"]), losses=int(stats["losses"]),
                streak=stats["streak"], division=division["name"]))

    from data.weather import Weather
    weather = Weather(temp=54, feels=51, high=63, low=44, wind=9,
                      icon="rain", text="RAIN", unit="F")

    store = FakeStore(games, rankings_map, standings_map, weather)
    ctx = RenderContext(config, store, 64, 32)
    return ctx, games


def render(screen, elapsed=0.5, size=(64, 32)):
    frame = Frame(*size)
    screen.draw(frame, elapsed)
    return frame.image


def collect(ctx, games):
    """(label, image) for every screen worth eyeballing."""
    by_id = {g.id: g for g in games}
    shots = []

    def add(label, screen, elapsed=0.5):
        shots.append((label, render(screen, elapsed)))

    add("CFB live / red zone", game_screens.LiveGameScreen(ctx, by_id["401001"]), 0.1)
    add("CFB live / drive", game_screens.LiveGameScreen(ctx, by_id["401002"]))
    add("CFB halftime", game_screens.LiveGameScreen(ctx, by_id["401003"]))
    add("CFB pregame", game_screens.PregameScreen(ctx, by_id["401004"]))
    add("CFB countdown", game_screens.PregameScreen(ctx, by_id["401005"]))
    add("CFB final / OT", game_screens.FinalScreen(ctx, by_id["401006"]))
    add("CFB final", game_screens.FinalScreen(ctx, by_id["401007"]))
    add("NFL live / goal line", game_screens.LiveGameScreen(ctx, by_id["402001"]), 0.1)
    add("NFL pregame", game_screens.PregameScreen(ctx, by_id["402002"]))
    add("NFL final", game_screens.FinalScreen(ctx, by_id["402003"]))

    add("AP Top 25 p1", info_screens.RankingsScreen(ctx, "ncaaf", 0))
    add("AP Top 25 p2", info_screens.RankingsScreen(ctx, "ncaaf", 1))
    add("Standings AFC N", info_screens.StandingsScreen(ctx, "nfl", "AFC North"))
    add("Leaders", info_screens.LeadersScreen(ctx, by_id["401001"]))
    add("Team card (live)", info_screens.TeamCardScreen(ctx, "ncaaf", "OSU"))
    add("Team card (NFL)", info_screens.TeamCardScreen(ctx, "nfl", "CIN"))
    add("Ticker", info_screens.TickerScreen(ctx, games), 2.0)
    add("Clock", info_screens.ClockScreen(ctx))
    add("Weather", info_screens.WeatherScreen(ctx), 1.0)
    add("Offseason", info_screens.MessageScreen(ctx, "NO GAMES", "CHECK BACK LATER"))
    return shots


def contact_sheet(shots, scale=6, columns=3):
    label_h = 14
    cell_w, cell_h = 64 * scale, 32 * scale + label_h
    pad = 10
    rows = (len(shots) + columns - 1) // columns
    sheet = Image.new("RGB",
                      (columns * cell_w + pad * (columns + 1),
                       rows * cell_h + pad * (rows + 1)),
                      (24, 26, 30))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    except Exception:
        font = ImageFont.load_default()

    for i, (label, image) in enumerate(shots):
        col, row = i % columns, i // columns
        x = pad + col * (cell_w + pad)
        y = pad + row * (cell_h + pad)
        draw.text((x, y), label, fill=(190, 200, 210), font=font)
        big = image.resize((64 * scale, 32 * scale), Image.NEAREST)
        sheet.paste(big, (x, y + label_h))
        draw.rectangle([x, y + label_h, x + cell_w - 1, y + label_h + 32 * scale - 1],
                       outline=(60, 66, 74))
    return sheet


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=int, default=6)
    parser.add_argument("--columns", type=int, default=3)
    parser.add_argument("--individual", action="store_true",
                        help="also write one PNG per screen")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    ctx, games = build_context()
    shots = collect(ctx, games)

    sheet = contact_sheet(shots, scale=args.scale, columns=args.columns)
    path = os.path.join(OUT_DIR, "contact_sheet.png")
    sheet.save(path)
    print("wrote {} ({} screens)".format(path, len(shots)))

    if args.individual:
        for label, image in shots:
            safe = label.lower().replace(" ", "_").replace("/", "")
            image.resize((64 * args.scale, 32 * args.scale), Image.NEAREST).save(
                os.path.join(OUT_DIR, "{}.png".format(safe)))


if __name__ == "__main__":
    main()
