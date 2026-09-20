"""Save real ESPN responses to disk so layouts can be developed offline.

    python3 -m tools.capture ncaaf nfl

Then point the app at them:

    SCOREBOARD_FIXTURES=fixtures python3 main.py --backend emulator

Useful for reproducing a specific game state: capture during a live red-zone
drive, then replay it in July while fixing the layout.
"""

import os
import sys

from data import espn, leagues

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "fixtures")


def capture(keys):
    os.makedirs(OUT, exist_ok=True)
    for key in keys:
        league = leagues.get(key)
        if not league:
            print("unknown league: {}".format(key))
            continue

        url = "{}/{}/scoreboard".format(espn.BASE, league.path)
        if league.query:
            url += "?" + league.query
        try:
            payload = espn.save_fixture(url, OUT)
            print("{:8} scoreboard: {} events".format(
                key, len(payload.get("events", []))))
        except Exception as exc:
            print("{:8} scoreboard failed: {}".format(key, exc))

        if league.rankings_path:
            try:
                espn.save_fixture(
                    "{}/{}/rankings".format(espn.BASE, league.rankings_path), OUT)
                print("{:8} rankings ok".format(key))
            except Exception as exc:
                print("{:8} rankings failed: {}".format(key, exc))

        if league.standings_path:
            try:
                espn.save_fixture(
                    "{}/{}/standings".format(espn.BASE, league.standings_path), OUT)
                print("{:8} standings ok".format(key))
            except Exception as exc:
                print("{:8} standings failed: {}".format(key, exc))

    print("\nfixtures in {}".format(OUT))


if __name__ == "__main__":
    capture(sys.argv[1:] or ["nfl", "ncaaf"])
