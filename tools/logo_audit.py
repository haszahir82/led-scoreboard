"""Which teams are falling back to text, and why.

Noticing them one at a time is the slow way. This walks every team in the
leagues you have enabled, runs each one through the real pipeline, and reports
the ones that will draw their abbreviation instead of a mark -- with the
numbers that decided it, so the answer is a reason rather than a verdict.

Run it on the Pi: it needs ESPN, and it has to see the same override folder and
the same cache the board reads.

    python3 -m tools.logo_audit                 every enabled league
    python3 -m tools.logo_audit --league ncaaf  one league
    python3 -m tools.logo_audit --today         only teams playing today
    python3 -m tools.logo_audit --verbose       every team, not just failures

What to do with a failure depends on which column is small:

    lit near zero      the mark is genuinely absent on a black panel. Nothing
                       to recover; give it override art or leave it as text.
    lit fine, solid 0  the mark is there but dark, or it is a saturated blue
                       or purple that luminance underrates. The rescue pass
                       should already have caught it -- if it did not, the
                       shape is probably a thin outline.
    no artwork         ESPN has no logo for that abbreviation, which usually
                       means the abbreviation is not what ESPN calls them.

For anything worth hand-tuning:  ./scoreboard tune ABBR --league X

A college run checks every team ESPN lists, which is around 760 including NAIA,
junior-college and Canadian programmes. Most of the ones with no artwork are
those, and they will never reach your board -- so they are counted rather than
listed unless you ask for --verbose.
"""

import argparse
import sys

from data import espn, leagues, logos


def team_artwork(league_key):
    """Every team in a league with its standard logo URL, from the teams API.

    The scoreboard endpoint only carries teams with a game scheduled, so an
    audit run on a Tuesday would report a handful of teams and look complete.
    """
    league = leagues.LEAGUES[league_key]
    payload = espn._get("{}/{}/teams?limit=1000".format(espn.BASE, league.path))
    out = []
    for sport in payload.get("sports", []):
        for entry in sport.get("leagues", []):
            for item in entry.get("teams", []):
                team = item.get("team", {})
                art = team.get("logos") or []
                std = next((l["href"] for l in art
                            if "dark" not in l.get("href", "")), "")
                out.append((str(team.get("abbreviation", "")).upper(),
                            team.get("displayName", ""),
                            std or (art[0]["href"] if art else "")))
    return sorted(out)


def playing_today(league_key):
    try:
        games = espn.scoreboard(league_key)
    except Exception:
        return set()
    wanted = set()
    for game in games:
        for team in game.teams:
            wanted.add(team.abbr.upper())
    return wanted


def audit(league_key, only_today=False, size=20):
    teams = team_artwork(league_key)
    if only_today:
        today = playing_today(league_key)
        teams = [t for t in teams if t[0] in today]

    rows = []
    for abbr, name, url in teams:
        logos.clear_memory()
        if not url:
            rows.append((abbr, name, None, 0.0, 0.0, 0.0, "no artwork from ESPN"))
            continue
        img = logos.get(abbr, url=url, size=size, league=league_key)
        if img is None:
            # Re-fetch without the gate to report what it would have drawn.
            logos.clear_memory()
            raw = logos.get(abbr, url=url, size=size, league=league_key,
                            require_legible=False)
            if raw is None:
                rows.append((abbr, name, None, 0.0, 0.0, 0.0, "could not load"))
            else:
                rows.append((abbr, name, None,
                             100 * logos.visibility(raw),
                             100 * logos.readability(raw),
                             100 * logos.presence(raw),
                             "too faint to read"))
        else:
            rows.append((abbr, name, img,
                         100 * logos.visibility(img),
                         100 * logos.readability(img),
                         100 * logos.presence(img), "ok"))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", action="append", default=[],
                        help="repeatable; defaults to every enabled league")
    parser.add_argument("--today", action="store_true",
                        help="only teams with a game on today's slate")
    parser.add_argument("--verbose", action="store_true",
                        help="list every team, not only the failures")
    parser.add_argument("--size", type=int, default=20)
    args = parser.parse_args(argv)

    from data.config import Config
    config = Config()
    keys = args.league or list(config.enabled_leagues())
    if not keys:
        print("No leagues enabled in config.json.")
        return 1

    total_bad = 0
    for key in keys:
        if key not in leagues.LEAGUES:
            print("Unknown league: {}".format(key))
            continue
        print()
        print("{}  ({})".format(key, "today only" if args.today else "all teams"))
        try:
            rows = audit(key, only_today=args.today, size=args.size)
        except Exception as exc:
            print("  could not reach ESPN: {}".format(exc))
            continue

        # Two failures that look alike in a list and are nothing alike in
        # practice. A team whose artwork does not survive the shrink is
        # something you can fix; a team ESPN has no artwork for at all is not,
        # and in a college list that second group is ninety per cent of the
        # output. Reported together, the three findings that matter disappear
        # into ninety lines of NAIA programmes that will never reach a board.
        missing = [r for r in rows if r[6] == "no artwork from ESPN"]
        failing = [r for r in rows if r[6] not in ("ok", "no artwork from ESPN")]
        total_bad += len(failing)

        print("  {} teams checked".format(len(rows)))

        if failing:
            print()
            print("  Artwork that will not render  ({})".format(len(failing)))
            print("  {:<7} {:<30} {:>5} {:>6} {:>8}".format(
                "ABBR", "TEAM", "lit", "solid", "present"))
            for abbr, name, _, lit, solid, present, state in failing:
                print("  {:<7} {:<30} {:>4.0f}% {:>5.0f}% {:>7.0f}%".format(
                    abbr, name[:30], lit, solid, present))
            print("    Hand-tune any of these:")
            print("      ./scoreboard tune {} --league {}".format(
                failing[0][0], key))

        if missing:
            print()
            print("  No artwork from ESPN  ({})".format(len(missing)))
            print("    Nothing to fix here: ESPN serves no logo for these at")
            print("    all. In a college list they are almost entirely NAIA,")
            print("    junior-college and Canadian programmes that will not")
            print("    reach your board.{}".format(
                "" if args.verbose else " Use --verbose to list them."))
            if args.verbose:
                for abbr, name, _, _, _, _, _ in missing:
                    print("      {:<7} {}".format(abbr, name[:44]))

        # Abbreviations are how override art is filed, so two teams sharing one
        # in the same league is a trap: a file at logos/<league>/OSU.png would
        # be drawn for Ohio State AND for Ohio State Newark. The league-scoped
        # folders fixed collisions BETWEEN leagues; this is the same problem
        # inside one.
        seen = {}
        for abbr, name, _, _, _, _, _ in rows:
            seen.setdefault(abbr, []).append(name)
        clashes = {a: n for a, n in seen.items() if len(n) > 1}
        if clashes:
            print()
            print("  Shared abbreviations  ({})".format(len(clashes)))
            for abbr in sorted(clashes):
                print("    {:<7} {}".format(abbr, " / ".join(clashes[abbr])[:60]))
            print("    Override art is filed by abbreviation, so one file here")
            print("    would be used for every team sharing the name.")

        if not failing and not missing:
            print("  every team draws a logo")

    print()
    if total_bad:
        print("  lit     = bright enough to see at all")
        print("  solid   = bright enough to read as a shape (luminance)")
        print("  present = bright enough to read as a shape (distance from")
        print("            black, the fairer measure for blues and purples)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
