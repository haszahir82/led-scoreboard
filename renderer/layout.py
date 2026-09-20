"""Drawing primitives for a 64x32 (or larger) LED panel.

Everything is composed into a PIL image and pushed to the matrix in one
SetImage per frame. Drawing per-pixel through the matrix graphics API is
slower and makes alpha compositing of logos impossible.

Two rules govern every layout in this project:

  1. A 64x32 panel with two 20px logos leaves a 24px centre column. Anything
     that has to live between the logos must fit in 24px, which is four
     characters of the small font. That constraint, not taste, is why the
     score screens look the way they do.
  2. Colour on an LED panel is not colour on a monitor. Pure blue reads as
     near-black and mid greys read as muddy, so the palette below is
     deliberately high-value and saturated.
"""

import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(ROOT, "fonts")

# ---------------------------------------------------------------- palette

WHITE = (255, 255, 255)
DIM = (140, 140, 140)
DARK = (70, 70, 70)
BLACK = (0, 0, 0)
RED = (255, 40, 40)
GREEN = (60, 235, 90)
YELLOW = (255, 205, 40)
ORANGE = (255, 140, 30)
CYAN = (60, 220, 235)
BLUE = (90, 140, 255)          # lifted; true blue disappears on LEDs
PURPLE = (185, 110, 255)
MAGENTA = (255, 80, 200)

POSSESSION = YELLOW
REDZONE = RED
LIVE = GREEN
RANK = YELLOW


def hex_color(value, fallback=WHITE, min_luma=70):
    """ESPN team hex -> RGB, brightened when too dark to read on a panel."""
    try:
        value = (value or "").lstrip("#")
        rgb = tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback
    luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    if luma < min_luma:
        scale = (min_luma + 40) / max(luma, 1)
        rgb = tuple(min(255, int(c * scale)) for c in rgb)
    return rgb


# ---------------------------------------------------------------- fonts

_font_cache = {}


def font(name, size):
    key = (name, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    return _font_cache[key]


class Fonts:
    """Named roles rather than filenames, so a font swap is a one-line change."""

    @property
    def tiny(self):        # ~4px caps, for labels and records
        return font("CG pixel 3x5.ttf", 5)

    @property
    def small(self):       # ~6px, the workhorse
        return font("04B_24__.TTF", 8)

    @property
    def small_alt(self):   # slightly wider, better for mixed text
        return font("04B_03__.TTF", 8)

    @property
    def medium(self):
        return font("04B_24__.TTF", 16)

    @property
    def score(self):       # big numerals
        return font("score_large.otf", 16)

    @property
    def score_small(self):
        return font("score_large.otf", 12)


FONTS = Fonts()


def text_size(draw, string, fnt):
    """(width, height) that works across Pillow versions."""
    try:
        box = draw.textbbox((0, 0), string, font=fnt)
        return box[2] - box[0], box[3] - box[1]
    except AttributeError:                      # Pillow < 8
        return fnt.getsize(string)


def text_width(draw, string, fnt):
    return text_size(draw, string, fnt)[0]


def fit(draw, string, fnt, max_width, ellipsis=False):
    """Trim a string until it fits max_width."""
    if text_width(draw, string, fnt) <= max_width:
        return string
    trimmed = string
    while trimmed and text_width(draw, trimmed, fnt) > max_width:
        trimmed = trimmed[:-1]
    if ellipsis and len(trimmed) > 1:
        trimmed = trimmed[:-1] + "."
    return trimmed


class Frame:
    """A drawable RGB frame with panel-aware helpers."""

    def __init__(self, width=64, height=32):
        self.width = width
        self.height = height
        self.image = Image.new("RGB", (width, height), BLACK)
        self.draw = ImageDraw.Draw(self.image)

    # ---- text ----

    def text(self, xy, string, fnt=None, fill=WHITE):
        fnt = fnt or FONTS.small
        self.draw.text(xy, string, font=fnt, fill=fill)

    def text_centered(self, y, string, fnt=None, fill=WHITE, x0=0, x1=None):
        """Centre within [x0, x1), defaulting to the whole panel."""
        fnt = fnt or FONTS.small
        x1 = self.width if x1 is None else x1
        width = text_width(self.draw, string, fnt)
        x = x0 + max(0, ((x1 - x0) - width) // 2)
        self.draw.text((x, y), string, font=fnt, fill=fill)
        return width

    def text_right(self, xy, string, fnt=None, fill=WHITE):
        fnt = fnt or FONTS.small
        width = text_width(self.draw, string, fnt)
        self.draw.text((xy[0] - width, xy[1]), string, font=fnt, fill=fill)
        return width

    def text_fitted(self, xy, string, fnt, fill, max_width, ellipsis=True):
        self.text(xy, fit(self.draw, string, fnt, max_width, ellipsis), fnt, fill)

    # ---- shapes ----

    def rect(self, box, fill=None, outline=None):
        self.draw.rectangle(box, fill=fill, outline=outline)

    def hline(self, y, x0=0, x1=None, fill=DARK):
        x1 = self.width - 1 if x1 is None else x1
        self.draw.line([(x0, y), (x1, y)], fill=fill)

    def vline(self, x, y0=0, y1=None, fill=DARK):
        y1 = self.height - 1 if y1 is None else y1
        self.draw.line([(x, y0), (x, y1)], fill=fill)

    def pixel(self, x, y, fill=WHITE):
        if 0 <= x < self.width and 0 <= y < self.height:
            self.draw.point((x, y), fill=fill)

    def dot(self, x, y, fill=WHITE, size=2):
        self.draw.rectangle([x, y, x + size - 1, y + size - 1], fill=fill)

    # ---- images ----

    def paste(self, img, xy):
        if img is None:
            return
        if img.mode == "RGBA":
            self.image.paste(img, xy, img)
        else:
            self.image.paste(img, xy)

    def logo_or_abbr(self, img, xy, abbr, box=20, color=WHITE):
        """Draw a logo, or fall back to the team abbreviation in team colour.

        The fallback matters: the first time an unranked MAC team appears the
        logo will not be cached yet, and a blank square looks broken while
        four letters looks intentional.
        """
        if img is not None:
            self.paste(img, xy)
            return
        x, y = xy
        fnt = FONTS.small if len(abbr) <= 4 else FONTS.tiny
        width = text_width(self.draw, abbr, fnt)
        self.draw.text((x + max(0, (box - width) // 2), y + box // 2 - 3),
                       abbr, font=fnt, fill=color)
