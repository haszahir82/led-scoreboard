"""Fix one team's logo when ESPN's artwork does not survive the shrink.

Most marks come down to 20px fine. A few do not: a thin outline averages away,
a wordmark turns to mush, a crest with six colours becomes a smudge. The board
already falls back to the abbreviation when a logo fails the legibility check,
but "OSU in scarlet" is not what you want if the real mark would work with a
different treatment.

This produces a contact sheet of the same logo under every treatment the
renderer can apply, at panel size and again at 6x so you can actually see it,
then writes whichever one you pick into the override folder. From then on the
board uses your file and never asks ESPN for that team again.

    python3 -m tools.logo_tune OSU --league ncaaf
    python3 -m tools.logo_tune OSU --league ncaaf --pick 3

Run it on the Pi. It needs ESPN, and the override it writes has to land in the
copy of logos/ that the board actually reads.
"""

import argparse
import io
import os
import sys

from PIL import Image, ImageEnhance

from data import espn, logos

PREVIEW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "state", "logo-preview.png")


def team_logo_url(abbr: str, league_key: str):
    """Find a team's artwork URL without needing them to be playing today.

    The scoreboard endpoint only carries teams with a game on the schedule, so
    a Tuesday in June returns nothing and the tool looks broken. The teams
    endpoint always has everyone.
    """
    from data import leagues
    league = leagues.LEAGUES[league_key]
    payload = espn._get("{}/{}/teams?limit=1000".format(espn.BASE, league.path))
    want = abbr.upper()
    for sport in payload.get("sports", []):
        for entry in sport.get("leagues", []):
            for item in entry.get("teams", []):
                team = item.get("team", {})
                if str(team.get("abbreviation", "")).upper() != want:
                    continue
                art = team.get("logos") or []
                std = next((l["href"] for l in art
                            if "dark" not in l.get("href", "")), "")
                return std or (art[0]["href"] if art else ""), \
                    team.get("displayName", want)
    return "", want


def coverage(img: Image.Image):
    """How much of this 20px mark survives on an unlit panel.

    Two numbers, because one is not enough. "Lit" is how much of the mark is
    bright enough to see at all; a predominantly black logo on a black panel
    reads as its fragments, and the dark-boost cannot help because it scales V
    in HSV and zero times anything is still zero. "Solid" is how much is bright
    enough to read as the shape rather than as its shadow -- Ohio State's
    true-colour Block O is 37% lit but 5% solid, a scarlet haze with no letter
    in it. Both come straight from data/logos.py so the figures here are the
    ones the board itself acts on.
    """
    return {
        "visible_pct": 100.0 * logos.visibility(img),
        "strong_pct": 100.0 * logos.readability(img),
    }


def board_choice(url: str, size: int):
    """Which of ESPN's two assets the board will actually use, and why.

    Runs the real rule from data/logos.py over both variants, so this answers
    the question "why does my team look like that" without guessing.
    """
    import requests

    def render(candidate):
        if not candidate:
            return None
        try:
            resp = requests.get(candidate, timeout=10,
                                headers={"User-Agent": "led-scoreboard/2.25"})
            resp.raise_for_status()
            return logos._prepare(Image.open(io.BytesIO(resp.content)), size)
        except Exception:
            return None

    std = render(url)
    standard = logos.readability(std) if std is not None else -1.0
    if not logos.needs_second_look(standard):
        return ("true colour", standard, None,
                "reads well enough on its own; the redraw is never fetched")

    dark_url = logos.dark_variant(url)
    alt = render(dark_url)
    if alt is None:
        return ("true colour", standard, None,
                "no dark redraw exists for this team" if not dark_url
                else "the dark redraw could not be fetched")

    dark = logos.readability(alt)
    if logos.prefers_dark(standard, dark):
        return ("dark redraw", standard, dark,
                "{:.0f}% solid beats {:.0f}% by more than the {:.0f}% margin"
                .format(100 * dark, 100 * max(0.0, standard),
                        100 * logos.DARK_VARIANT_MARGIN))
    return ("true colour", standard, dark,
            "the redraw's {:.0f}% does not clear {:.0f}% by the {:.0f}% margin, "
            "so the real colours are kept".format(
                100 * dark, 100 * max(0.0, standard),
                100 * logos.DARK_VARIANT_MARGIN))


def describe(source: Image.Image):
    """What the pipeline sees in this source, and what it will do about it."""
    rgba = source.convert("RGBA")
    colours = rgba.convert("RGB").getcolors(maxcolors=100000)
    n = len(colours) if colours else 100000
    low, high = rgba.split()[-1].getextrema()
    chosen = logos._resample_for(rgba)
    return {
        "size": rgba.size,
        "colours": n,
        "alpha": (low, high),
        "cutout": low <= logos.ALPHA_CLEAR,
        "filter": "NEAREST" if chosen == Image.NEAREST else "LANCZOS",
        "why": ("treated as pixel art: {} colours is at or under the {} threshold"
                .format(n, logos.PIXEL_ART_COLOURS) if chosen == Image.NEAREST
                else "treated as artwork: {} colours is over the {} threshold"
                .format(n, logos.PIXEL_ART_COLOURS)),
    }


def treatments(source: Image.Image, size: int):
    """Every knob that matters, named so a pick is readable.

    The resample filter is first, because it is the one that decides whether a
    mark survives the shrink at all. The rest -- saturation, contrast, the
    dark-boost, the trim -- only adjust a picture that already reads.

    These mirror what data/logos.py does rather than inventing new processing,
    so what you see here is what the board will draw.
    """
    out = []

    def render(label, saturation, contrast, boost, trim, resample):
        img = source.convert("RGBA")
        if trim:
            box = logos._content_bbox(img)
            if box:
                img = img.crop(box)
        side = max(img.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
        square = square.resize((size, size), resample)
        flat = Image.new("RGB", (size, size), (0, 0, 0))
        flat.paste(square, (0, 0), square)
        flat = ImageEnhance.Color(flat).enhance(saturation)
        flat = ImageEnhance.Contrast(flat).enhance(contrast)
        if boost:
            flat = logos._boost_dark(flat)
        out.append((label, flat))

    auto = logos._resample_for(source.convert("RGBA"))
    auto_name = "NEAREST" if auto == Image.NEAREST else "LANCZOS"

    render("as the board draws it now ({})".format(auto_name),
           logos.SATURATION, logos.CONTRAST, True, True, auto)
    render("forced LANCZOS (averaged, for detailed marks)",
           logos.SATURATION, logos.CONTRAST, True, True, Image.LANCZOS)
    render("forced NEAREST (sampled, for flat pixel art)",
           logos.SATURATION, logos.CONTRAST, True, True, Image.NEAREST)
    render("forced BOX (area average, no ringing)",
           logos.SATURATION, logos.CONTRAST, True, True, Image.BOX)
    render("LANCZOS, no brightness boost",
           logos.SATURATION, logos.CONTRAST, False, True, Image.LANCZOS)
    render("LANCZOS, punchier",
           1.8, 1.35, True, True, Image.LANCZOS)
    return out


def contact_sheet(items, size, scale=6):
    """Panel-size on the left, magnified on the right, one row per treatment."""
    pad, row_h = 8, max(size * scale, 24) + 8
    sheet = Image.new("RGB", (size + size * scale + pad * 3,
                              row_h * len(items) + pad), (16, 16, 16))
    for index, (_, img) in enumerate(items):
        y = pad // 2 + index * row_h
        sheet.paste(img, (pad, y + (row_h - size) // 2 - 4))
        sheet.paste(img.resize((size * scale, size * scale), Image.NEAREST),
                    (pad * 2 + size, y))
    return sheet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("abbr", help="team abbreviation as ESPN spells it, e.g. OSU")
    parser.add_argument("--league", default="ncaaf")
    parser.add_argument("--size", type=int, default=20)
    parser.add_argument("--url", default="", help="skip the lookup, use this image")
    parser.add_argument("--dark", action="store_true",
                        help="start from ESPN's dark-background redraw instead")
    parser.add_argument("--pick", type=int, default=0,
                        help="write treatment N to the override folder")
    args = parser.parse_args(argv)

    url, name = (args.url, args.abbr) if args.url \
        else team_logo_url(args.abbr, args.league)
    if not url:
        print("No artwork found for {} in {}. Check the abbreviation against "
              "what ESPN uses.".format(args.abbr, args.league))
        return 1
    if args.dark:
        url = logos.dark_variant(url) or url

    import requests
    resp = requests.get(url, timeout=15,
                        headers={"User-Agent": "led-scoreboard/2.14"})
    resp.raise_for_status()
    source = Image.open(io.BytesIO(resp.content))

    facts = describe(source)
    print("{} ({})".format(name, url))
    print("  source    : {}x{}, {} colours, alpha {}".format(
        facts["size"][0], facts["size"][1], facts["colours"], facts["alpha"]))
    print("  transparent: {}".format("yes, real cutout" if facts["cutout"]
                                     else "no, trimmed on brightness"))
    print("  resample  : {}".format(facts["filter"]))
    print("              {}".format(facts["why"]))

    if not args.dark and not args.url:
        pick, standard, dark, why = board_choice(url, args.size)
        print("  board uses: {}".format(pick))
        print("              {}".format(why))
        if dark is not None:
            print("              true colour {:.0f}% solid, redraw {:.0f}% solid"
                  .format(100 * max(0.0, standard), 100 * dark))

    items = treatments(source, args.size)

    if args.pick:
        if not 1 <= args.pick <= len(items):
            print("Pick between 1 and {}.".format(len(items)))
            return 1
        label, img = items[args.pick - 1]
        folder = logos.OVERRIDE_DIR if args.league == "nfl" \
            else os.path.join(logos.OVERRIDE_DIR, args.league)
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, "{}.png".format(args.abbr.upper()))
        img.save(dest)
        logos.clear_memory()
        print("Wrote {} ({})".format(dest, label))
        print("Clear the cache and restart so the board picks it up:")
        print("  rm -rf ~/scoreboard/logo_cache && ./scoreboard restart")
        return 0

    os.makedirs(os.path.dirname(PREVIEW), exist_ok=True)
    contact_sheet(items, args.size).save(PREVIEW)
    print()
    for index, (label, img) in enumerate(items, 1):
        cov = coverage(img)
        print("  {}. {:<44} lit {:>4.0f}%  solid {:>4.0f}%  legible={}".format(
            index, label, cov["visible_pct"], cov["strong_pct"],
            "yes" if logos.legible(img) else "NO"))
    print()
    print("  lit    = share of the mark bright enough to see on an unlit panel")
    print("  solid  = share bright enough to read as shape rather than shadow")
    print("  Under {:.0f}% lit the board gives up and draws the abbreviation."
          .format(100 * logos.MIN_BRIGHT_RATIO))
    print("  Under about 20% solid the mark is there but shapeless, which is")
    print("  what a hand-made override is for.")
    print()
    print("Contact sheet: {}".format(PREVIEW))
    print("Open it at http://<board>.local:8080/state/logo-preview.png, or")
    print("scp it to your Mac. Then re-run with --pick N to keep one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
