"""On-demand team logo cache.

Upstream ships ~90 hand-made NFL PNGs. That does not scale to 130+ FBS teams
plus whatever other leagues get switched on, so logos are pulled from ESPN's
CDN once and cached to disk at the exact pixel size the panel needs.

Two things matter when shrinking a 500px logo to 20px on an LED panel:

  * Resize on the alpha-composited image, not the raw RGBA. Otherwise the
    transparent border pixels (which are usually black) bleed into the edges
    and every logo ends up with a dark halo.
  * Boost saturation and contrast afterwards. LEDs at low duty cycle wash out,
    and a 20px logo has no room for subtle shading to survive.

A hand-drawn override in logos/ always wins, so the good NFL helmet art from
upstream is still used where it exists.
"""

import hashlib
import os
import re
import threading

from PIL import Image, ImageEnhance

try:
    import requests
except ImportError:                          # pragma: no cover
    requests = None

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "logo_cache")
OVERRIDE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "logos")

_lock = threading.Lock()
_memory = {}
_failed = set()

SATURATION = 1.35
CONTRAST = 1.15

# A logo darker than this is invisible on an unlit panel, so it gets brightened
# until its lightest pixel reaches TARGET_MAX, capped at MAX_GAIN so a nearly
# black mark does not become grey mush.
TARGET_MAX = 170
MAX_GAIN = 3.5

# Below this fraction of pixels bright enough to see, the logo is not a logo
# any more and the caller is better off drawing the abbreviation.
#
# This was 0.03, which is very nearly no bar at all. Cincinnati's mark came
# through at 4% lit -- 96% of it invisible against an unlit panel -- and passed,
# so the board drew a few scattered pixels instead of falling back to four
# legible letters. Measured against real artwork, under roughly 12% there is
# nothing on the panel a person could name.
MIN_BRIGHT_RATIO = 0.12
BRIGHT_THRESHOLD = 45

# A standard logo has to be this much worse than ESPN's dark redraw before the
# redraw wins. The true-colour mark is preferred where both are readable --
# scarlet is the point of a scarlet logo -- but not at the cost of showing
# almost nothing, which is what a fixed preference produced.
DARK_VARIANT_MARGIN = 0.10

# Pixels this bright read as the shape itself rather than as its shadow.
# Ohio State's true-colour mark is 37% lit but only 5% solid -- a scarlet haze
# where a Block O should be -- while its dark redraw is 32% solid. "Lit" alone
# could not tell those apart, so the variant choice is scored on this instead.
SOLID_THRESHOLD = 110

# Above this much solid coverage a mark reads properly, and there is no reason
# to spend a second HTTP request asking whether the redraw reads better still.
CLEARLY_READABLE = 0.22


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def dark_variant(url: str):
    """ESPN's light-outlined logo, meant for dark backgrounds.

    Many marks are drawn to sit on white: a navy roundel, or a shape whose
    definition comes from a thin light keyline. Shrunk to 20px on a black
    panel, the keyline averages away and the mark becomes a silhouette. The
    dark variant is the same logo redrawn to hold up on a dark ground, which
    is exactly the situation here.
    """
    if not url or "-dark/" in url:
        return None
    if "/500/" in url:
        return url.replace("/500/", "/500-dark/")
    return None


def _boost_dark(img: Image.Image) -> Image.Image:
    """Lift a logo that would otherwise disappear, without draining its colour.

    The previous version scaled every channel by one factor until the image's
    *luminance* peak reached the target. On a mark that is mostly a single
    saturated dark colour -- Indiana's crimson, Alabama's crimson, Texas'
    burnt orange -- luminance is low (crimson reads about 69 of 255) while the
    red channel is already near full, so the gain came out around 2.5x. Red
    clipped at 255 while green and blue climbed from almost nothing to a third
    of scale, which is pink. With the saturation and contrast passes ahead of
    it, near enough to white on a 20px mark.

    Teams whose logo contained any white pixel had a luminance peak of 255
    already and were never boosted at all, which is why only some of the red
    logos looked wrong and it read like an ESPN artwork problem.

    Working in HSV and raising only V leaves hue and saturation untouched, so a
    dark logo gets brighter rather than paler.
    """
    hue, sat, val = img.convert("HSV").split()
    _, high = val.getextrema()
    if not high or high >= TARGET_MAX:
        return img
    gain = min(MAX_GAIN, TARGET_MAX / float(high))
    val = val.point(lambda p: min(255, int(p * gain)))
    return Image.merge("HSV", (hue, sat, val)).convert("RGB")


def visibility(img: Image.Image) -> float:
    """Fraction of the mark bright enough to see on an unlit panel.

    The single number that decides whether a logo is worth drawing, and which
    of ESPN's two versions of it to use. A panel is black when it is off, so
    the dark parts of a logo are not dark, they are absent -- and a mark that
    is mostly dark is mostly absent no matter what else is done to it. The
    brightness boost cannot rescue that either: it scales V in HSV, and zero
    times anything is still zero.
    """
    grey = img.convert("L")
    bright = sum(grey.histogram()[BRIGHT_THRESHOLD:])
    return bright / float(img.width * img.height)


def readability(img: Image.Image) -> float:
    """Fraction of the mark solid enough to read as a shape.

    Separate from visibility() on purpose, and the two disagree in exactly the
    case that matters. A mark can be widely lit and still shapeless: Ohio
    State's true-colour Block O measures 37% lit against 5% solid, which on the
    panel is a scarlet smear with no letter in it. Its dark redraw measures 32%
    solid. Choosing between two versions of a logo needs the second number.
    """
    grey = img.convert("L")
    solid = sum(grey.histogram()[SOLID_THRESHOLD:])
    return solid / float(img.width * img.height)


def legible(img: Image.Image) -> bool:
    """Would this actually read as something on an unlit panel?"""
    return visibility(img) >= MIN_BRIGHT_RATIO


def needs_second_look(standard: float) -> bool:
    """Is the true-colour mark doubtful enough to be worth a second request?"""
    return standard < CLEARLY_READABLE


def prefers_dark(standard: float, dark: float) -> bool:
    """Should ESPN's dark redraw replace the true-colour mark?

    Kept as a function over two numbers rather than buried in the fetch, so
    the rule can be checked against real measurements off the panel instead of
    inferred from the shape of the code around it. A negative `standard` means
    the true-colour asset did not load at all, in which case anything beats it.
    """
    if standard < 0:
        return dark >= 0
    return dark > standard + DARK_VARIANT_MARGIN


# Above this many distinct colours a mark is artwork; at or below it, it is
# pixel art. The bundled helmets all sit at three to five, the vector wordmarks
# start around forty and run into the thousands, so the two classes separate
# cleanly with nothing near the line.
PIXEL_ART_COLOURS = 8

# Luminance at or below this counts as background when a mark has no usable
# alpha channel. Kept low so a genuinely black element inside a logo is never
# mistaken for padding.
BACKGROUND_LEVEL = 12

# Alpha at or below this counts as genuinely transparent. Above it everywhere
# means the channel is a wash rather than a cutout, and says nothing useful
# about where the mark ends.
ALPHA_CLEAR = 16


def _resample_for(img: Image.Image):
    """Filter the artwork, sample the pixel art.

    The bundled helmets are hand-drawn at roughly 20x20 and stored as a 400x400
    upscale, so every edge in them is a hard block boundary. LANCZOS is an
    interpolating filter meant for photographs: run it across those blocks and
    they average into one another, and a five-colour helmet arrives on the
    panel as an orange smudge with a grey box in the corner. Sampling one pixel
    per block instead keeps it a helmet.

    The averaging is exactly right for everything else, though, so this is not
    a filter to apply globally. The Ravens mark sampled with NEAREST comes apart
    into disconnected specks and Green Bay's oval turns into a dotted ring,
    because the thin strokes in them fall between sample points and vanish.
    """
    colours = img.convert("RGB").getcolors(maxcolors=PIXEL_ART_COLOURS)
    return Image.NEAREST if colours is not None else Image.LANCZOS


def _content_bbox(img: Image.Image):
    """The mark's real extent, whether its margin is transparent or black.

    Trimming on alpha alone silently did nothing for the bundled helmets: they
    are fully opaque, with the margin painted black rather than left clear, so
    the alpha bbox is the whole canvas. The helmet then kept its padding
    through the resize and landed noticeably smaller than every mark beside it.
    """
    alpha = img.split()[-1]
    low, _ = alpha.getextrema()
    # Only trust alpha when some of it is actually transparent. Green Bay's
    # mark has an alpha channel that never drops below 191 -- a faint wash over
    # the whole canvas rather than a cutout -- so "has an alpha channel" was
    # the wrong question and trimming on it returned the full square, leaving
    # the oval floating in its own padding.
    if low <= ALPHA_CLEAR:
        return alpha.getbbox() or img.getbbox()

    # Opaque art: find the content by brightness instead. Not with getbbox(),
    # which trims only exactly-zero pixels -- the bundled helmets are painted
    # on (2, 2, 2) rather than pure black, so that found nothing at all and the
    # margin rode through the resize untouched. A small threshold catches the
    # near-black that image editors actually produce.
    lit = img.convert("L").point(lambda v: 255 if v > BACKGROUND_LEVEL else 0)
    return lit.getbbox() or img.getbbox()


def _prepare(img: Image.Image, size: int) -> Image.Image:
    """RGBA -> flattened, punchy RGB thumbnail at `size` square."""
    img = img.convert("RGBA")

    resample = _resample_for(img)

    # Trim the margin so the mark fills the box.
    bbox = _content_bbox(img)
    if bbox:
        img = img.crop(bbox)

    # Letterbox into a square so non-square marks are not stretched.
    side = max(img.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))

    square = square.resize((size, size), resample)

    flat = Image.new("RGB", (size, size), (0, 0, 0))
    flat.paste(square, (0, 0), square)

    flat = ImageEnhance.Color(flat).enhance(SATURATION)
    flat = ImageEnhance.Contrast(flat).enhance(CONTRAST)
    flat = _boost_dark(flat)
    return flat


def _cache_path(abbr: str, url: str, size: int, league: str) -> str:
    """Where a fetched logo is stored on disk.

    The name has to distinguish everything that changes the picture, and for a
    long time it did not: it was just the abbreviation and the size, so
    Cincinnati the Bengals and Cincinnati the Bearcats both wrote CIN_20.png.
    Whichever team the board fetched first owned the file, and the other one
    showed its logo from then on -- across restarts, because the collision is
    on disk. The in-memory cache is keyed properly and hid the problem inside a
    single run, which is why it took a person looking at the panel to find it.

    Miami, Houston, Las Vegas and San Diego all collide the same way. Keying on
    a digest of the source URL settles all of them at once and needs no list of
    known clashes: two teams cannot share a picture unless they share a URL, in
    which case they genuinely have the same logo. The league and abbreviation
    stay in the name so the folder is still readable by a human.
    """
    stamp = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:10]
    return os.path.join(CACHE_DIR, "{}_{}_{}_{}.png".format(
        _safe(league or "x"), _safe(abbr or "t"), size, stamp))


def _override_path(abbr: str, helmet: bool, league: str = ""):
    """Look for hand-made art before hitting the network.

    NFL art lives at the top of logos/, because that is where upstream's
    helmet PNGs already are and renaming them would strand anyone's existing
    folder. Every other league gets its own subfolder, logos/<league>/, which
    is what keeps MIA the Dolphins in one place and the Hurricanes in another.
    That collision is why college teams could not have override art at all
    before: the only folder available was the NFL one.

    Drop a PNG named after the team's ESPN abbreviation (OSU.png, NAVY.png) in
    the right folder and it wins over anything ESPN serves. Any size works,
    it gets resized like any other source, but art drawn at the panel's own
    size looks best because nothing has to be guessed on the way down.
    """
    # No league means no override art. Defaulting to the NFL folder here would
    # make every unrecognised league quietly inherit NFL helmets, which is the
    # exact collision this function exists to prevent.
    if not abbr or not league:
        return None
    league = league.lower()
    folder = OVERRIDE_DIR if league == "nfl" else os.path.join(OVERRIDE_DIR, league)
    candidates = []
    if helmet and league == "nfl":
        candidates += ["{}H.png".format(abbr), "{}H.PNG".format(abbr)]
    candidates += ["{}.png".format(abbr), "{}.PNG".format(abbr)]
    for name in candidates:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            return path
    return None


def get(abbr: str, url: str = "", size: int = 20, helmet: bool = False,
        flip: bool = False, league: str = "", overrides=None,
        require_legible: bool = True):
    """Return a size x size RGB logo, or None if it cannot be produced.

    Never raises and never blocks for long: a miss returns None and the caller
    falls back to drawing the team abbreviation, which is what should happen
    the first time an unknown team appears anyway.

    `league` selects which override folder is consulted, and getting it wrong
    is how a Dolphins helmet ends up on Miami of Ohio: plenty of NFL and
    college abbreviations collide (MIA, CIN, LV). An empty league means no
    override art at all, which is the safe default for a league we have not
    thought about yet.

    `overrides` is the old boolean spelling, kept so nothing breaks mid-upgrade.
    """
    if overrides is not None and not league:
        league = "nfl" if overrides else ""
    key = (abbr, url, size, helmet, flip, league, require_legible)
    if key in _memory:
        return _memory[key]
    if key in _failed:
        return None

    img = None
    override = _override_path(abbr, helmet, league) if league else None
    hand_picked = False
    if override:
        try:
            raw = Image.open(override)
            if raw.size == (size, size):
                hand_picked = True
                # Already drawn at panel size: use it exactly as it is.
                #
                # Running it through _prepare would apply the saturation,
                # contrast and dark-boost a SECOND time. Anything logo_tune
                # writes already has them baked in, so its output came back
                # oversaturated and lifted, and a hand-drawn 20x20 mark would
                # arrive in colours nobody picked. At panel size there is also
                # nothing left to resize, crop or rescue -- whoever drew it has
                # already made every decision this function would make.
                rgba = raw.convert("RGBA")
                flat = Image.new("RGB", (size, size), (0, 0, 0))
                flat.paste(rgba, (0, 0), rgba)
                img = flat
            else:
                img = _prepare(raw, size)
        except Exception:
            img = None

    if img is None and url:
        cache_path = _cache_path(abbr, url, size, league)
        try:
            if os.path.exists(cache_path):
                img = Image.open(cache_path).convert("RGB")
            elif requests is not None:
                os.makedirs(CACHE_DIR, exist_ok=True)
                from io import BytesIO

                # Pick the variant that is actually visible, by measuring both.
                #
                # Two earlier versions both got this wrong by deciding in
                # advance. Preferring the dark redraw always turned Indiana,
                # Alabama and Texas white, because for those schools the
                # "-dark" asset is the mark redrawn in flat white. Preferring
                # the true-colour one unless it failed the legibility gate was
                # no better, because the gate asked for 3% and Cincinnati's
                # mark clears that at 4% lit while being invisible.
                #
                # So neither is preferable in general. Render both, measure how
                # much of each survives on a black panel, and keep the better
                # one -- with a margin favouring true colour, because scarlet
                # is the point of a scarlet logo and the redraw should only win
                # when it wins clearly.
                def fetch(candidate):
                    if not candidate:
                        return None
                    try:
                        resp = requests.get(
                            candidate, timeout=6,
                            headers={"User-Agent": "led-scoreboard/2.1"})
                        resp.raise_for_status()
                        return _prepare(Image.open(BytesIO(resp.content)), size)
                    except Exception:
                        return None

                img = fetch(url)
                standard = readability(img) if img is not None else -1.0

                # Only pay for the second request when the first is doubtful.
                if needs_second_look(standard):
                    alt = fetch(dark_variant(url))
                    if alt is not None and \
                       prefers_dark(standard, readability(alt)):
                        img = alt

                if img is not None:
                    img.save(cache_path)
        except Exception:
            img = None

    # A mark too dark or too sparse to read is worse than no mark: the caller
    # draws the team abbreviation in team colour instead, which always reads.
    #
    # The important word is "next", not "no". This gate used to be the end of
    # the line, and because shipped override art is tried first and short
    # circuits everything after it, one bad file meant the abbreviation --
    # even when a perfectly good alternative was sitting in the same folder.
    #
    # Carolina is the case that showed it. The shipped CAR.png is the prowling
    # panther: a black cat with a thin blue keyline, which at 20px on an unlit
    # panel is 8% lit and 1% solid, a scatter of dim blue dots. It failed the
    # gate, so the board drew "CAR" -- while CARH.png, the silver helmet, sat
    # right beside it measuring 45% and 45%. Nothing ever looked at it.
    #
    # So a source that fails is a reason to try the next source, not a reason
    # to give up. Text is still the last resort, just no longer the second one.
    if img is not None and require_legible and not legible(img):
        if hand_picked:
            # Panel-sized override art comes from logo_tune, which means a
            # person looked at a contact sheet and chose this one. Measuring
            # it and overriding them defeats the point of the tool.
            pass
        else:
            img = _fallback_art(abbr, url, size, helmet, league)

    if img is None:
        with _lock:
            _failed.add(key)
        return None

    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)

    with _lock:
        _memory[key] = img
    return img


def _fallback_art(abbr, url, size, helmet, league):
    """The next thing to try when the preferred source will not read.

    Ordered by how much it is still the team's own mark. ESPN's artwork first,
    because that is the real logo and the variant measurement in get() may pick
    a redraw that works. The bundled helmet last, because a helmet is a
    different picture from a logo -- recognisably the right team, but not the
    thing that was asked for, so it is a substitute rather than a preference.

    Returns None when nothing reads, and then the caller draws the
    abbreviation, which always does.
    """
    for candidate in _fallback_sources(abbr, url, size, helmet, league):
        if candidate is not None and legible(candidate):
            return candidate
    return None


def _fallback_sources(abbr, url, size, helmet, league):
    """Each alternative source, loaded lazily so a miss costs nothing."""
    # ESPN, through the ordinary path, which includes choosing between the
    # true-colour asset and the dark redraw by measuring both.
    if url:
        try:
            yield get(abbr, url=url, size=size, helmet=helmet, league="",
                      require_legible=False)
        except Exception:
            yield None

    # The bundled helmet, for a league that has them.
    if not helmet and league:
        path = _override_path(abbr, True, league)
        if path:
            try:
                raw = Image.open(path)
                yield (raw.convert("RGB") if raw.size == (size, size)
                       else _prepare(raw, size))
            except Exception:
                yield None


def prefetch(games, size: int = 20, helmet: bool = False):
    """Warm the cache for a slate so the render loop never waits on HTTP."""
    for game in games:
        nfl = game.league == "nfl"
        for team in game.teams:
            try:
                get(team.abbr, team.logo_url, size=size,
                    helmet=helmet and nfl, league=game.league)
            except Exception:
                continue


def clear_memory():
    with _lock:
        _memory.clear()
        _failed.clear()
