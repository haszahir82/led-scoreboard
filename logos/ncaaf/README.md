# Override art for college teams

Drop a PNG here named after the team's ESPN abbreviation and the board uses it
instead of anything ESPN serves:

    logos/ncaaf/OSU.png
    logos/ncaaf/NAVY.png

Transparency is respected. Any size works, but art drawn at the panel's own
size (20x20 for the game screens, 16x16 for standings) looks best, because
nothing has to be guessed on the way down.

This folder is separate from the NFL art one directory up for a reason: plenty
of abbreviations collide. MIA is both the Dolphins and the Hurricanes, CIN is
both the Bengals and the Bearcats.

To build one from ESPN's own artwork with a different treatment, run this on
the board:

    python3 -m tools.logo_tune OSU --league ncaaf
    python3 -m tools.logo_tune OSU --league ncaaf --pick 3

The first command writes a contact sheet showing the same logo under every
treatment the renderer can apply; the second keeps the one you chose.
