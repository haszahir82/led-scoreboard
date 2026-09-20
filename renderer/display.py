"""Matrix abstraction and the main render loop.

The matrix binding is chosen at import time:

  * rgbmatrix          on the Pi (the real panel)
  * RGBMatrixEmulator  on a laptop, for layout work
  * a capture backend  for the screenshot tool and tests

The loop runs at a fixed frame rate and asks the current screen to draw itself
as a function of elapsed time. It never performs I/O, so a slow or failing
ESPN request cannot stutter an animation.
"""

import os
import time
import sys
import traceback
from datetime import datetime

from PIL import Image

from data import hardware
from renderer import layout
from renderer.layout import FONTS, Frame
from renderer.screens.base import resolve_tz

# Fallback only. The real frame rate comes from the detected board via
# data/hardware.py, so a Zero W runs at 8 and a Pi 3 A+ runs at 20 without
# anyone editing a constant.
FRAME_RATE = 20.0
MIN_FRAME_RATE = 2.0
MAX_FRAME_RATE = 60.0

# Last frame pushed to the panel, so the web UI can show a live preview
# without building a second render pipeline.
LATEST = {"image": None, "key": None, "at": 0.0}


def load_matrix(options, backend=None):
    """Return (matrix, canvas_factory) for the requested backend."""
    backend = backend or os.environ.get("SCOREBOARD_BACKEND", "auto")

    if backend in ("auto", "hardware"):
        try:
            from rgbmatrix import RGBMatrix, RGBMatrixOptions   # noqa
            return RGBMatrix(options=_to_options(RGBMatrixOptions, options)), "hardware"
        except ImportError:
            if backend == "hardware":
                raise

    if backend in ("auto", "emulator"):
        try:
            from RGBMatrixEmulator import RGBMatrix, RGBMatrixOptions
        except ImportError:
            # On a Pi this is the interesting case: the real library failed to
            # import and we fell through to a desktop emulator that is not
            # installed either. A bare ModuleNotFoundError for
            # RGBMatrixEmulator sends you looking for the wrong thing entirely,
            # so name the actual problem and the actual fix.
            raise RuntimeError(
                "No LED matrix backend available.\n"
                "  rgbmatrix (the real panel driver) is not importable, and\n"
                "  RGBMatrixEmulator (the desktop stand-in) is not installed.\n"
                "\n"
                "  On a Raspberry Pi, install the driver:\n"
                "    sudo apt-get install -y python-dev-is-python3 python3-pil "
                "cython3 cmake\n"
                "    sudo python3 -m pip install --break-system-packages ./matrix\n"
                "  or just re-run ./install.sh\n"
                "\n"
                "  For development on a desktop:\n"
                "    pip install RGBMatrixEmulator\n"
                "\n"
                "  Diagnose with: ./scoreboard doctor"
            )
        return RGBMatrix(options=_to_options(RGBMatrixOptions, options)), "emulator"

    raise RuntimeError("unknown backend {}".format(backend))


def _to_options(options_cls, values):
    options = options_cls()
    for key, value in values.items():
        if value is None:
            continue
        try:
            setattr(options, key, value)
        except Exception:
            pass
    return options


class RenderContext:
    """Everything a screen needs, passed down instead of reached up for.

    `width`/`height` are the CELL a screen draws into, not the whole panel.
    On a single 64x32 board they are the same thing; on a chained panel the
    cell stays 64x32 and the display tiles several of them.
    """

    def __init__(self, config, store, width=64, height=32, geometry=None):
        self.config = config
        self.store = store
        self.geometry = geometry
        self.width = width
        self.height = height

    @property
    def panel_width(self):
        return self.geometry.width if self.geometry else self.width

    @property
    def panel_height(self):
        return self.geometry.height if self.geometry else self.height

    @property
    def cell_count(self):
        return self.geometry.cell_count if self.geometry else 1

    @property
    def helmet_logos(self):
        return bool(self.config.get("display.use_helmet_logos", True))


class Display:
    def __init__(self, matrix, ctx, playlist, geometry=None):
        self.matrix = matrix
        self.ctx = ctx
        self.playlist = playlist
        self.geometry = geometry or ctx.geometry
        self.canvas = matrix.CreateFrameCanvas()
        self._screen_started = time.time()
        self._current_key = None
        self._brightness = None
        self._blank = False

    # ---------------------------------------------------------------- sleep

    def _sleeping(self):
        cfg = self.ctx.config.get("display.sleep") or {}
        if not cfg.get("enabled"):
            return False
        try:
            # Same timezone as everything else on screen, so "sleep at 23:30"
            # means 23:30 where you are, not where the Pi thinks it is.
            tz = resolve_tz(self.ctx.config.get("display.timezone", ""))
            now = datetime.now(tz).time()
            start = datetime.strptime(cfg.get("start", "23:30"), "%H:%M").time()
            end = datetime.strptime(cfg.get("end", "07:00"), "%H:%M").time()
        except ValueError:
            return False
        if start <= end:
            return start <= now < end
        return now >= start or now < end       # window crosses midnight

    def _apply_brightness(self):
        want = int(self.ctx.config.get("display.brightness", 60))
        if want != self._brightness:
            try:
                self.matrix.brightness = want
            except Exception:
                pass
            self._brightness = want

    # ---------------------------------------------------------------- loop

    @property
    def frame_rate(self):
        """Frames per second, from config or the detected board."""
        try:
            rate = float(hardware.tuned(self.ctx.config, "frame_rate")
                         or FRAME_RATE)
        except (TypeError, ValueError):
            rate = FRAME_RATE
        return max(MIN_FRAME_RATE, min(MAX_FRAME_RATE, rate))

    def run(self):
        rate = self.frame_rate
        version = self.ctx.config.version
        interval = 1.0 / rate

        while True:
            started = time.time()
            try:
                self.tick()
            except Exception as exc:
                self._draw_error(exc)

            # Re-read only when the config actually changed, so a saved
            # setting takes effect without a restart and without doing this
            # work on every frame.
            if self.ctx.config.version != version:
                version = self.ctx.config.version
                interval = 1.0 / self.frame_rate

            remaining = interval - (time.time() - started)
            if remaining > 0:
                time.sleep(remaining)

    def tick(self):
        if self._sleeping():
            if not self._blank:
                self.canvas.Clear()
                self.canvas = self.matrix.SwapOnVSync(self.canvas)
                self._blank = True
            time.sleep(1.0)
            return
        self._blank = False

        self._apply_brightness()
        self.playlist.maybe_rebuild()

        screen = self.playlist.current()
        if screen.key != self._current_key:
            self._current_key = screen.key
            self._screen_started = time.time()

        elapsed = time.time() - self._screen_started

        geo = self.geometry
        if geo is None or geo.single:
            frame = Frame(self.ctx.width, self.ctx.height)
            screen.draw(frame, elapsed)
        else:
            frame = self._draw_tiled(geo, elapsed)

        self._draw_status(frame)
        self.push(frame)

        if elapsed >= screen.duration and len(self.playlist) > 1:
            nxt = self.playlist.advance()
            self._current_key = nxt.key
            self._screen_started = time.time()

    def _draw_tiled(self, geo, elapsed):
        """Fill every 64x32 cell with a different screen from the playlist.

        Each cell is drawn into its own Frame and pasted, so no layout knows
        or cares that it is one of several. A cell whose screen raises still
        leaves the others intact, which matters when one game has odd data.
        """
        panel = Frame(geo.width, geo.height)
        screens = self.playlist.window(geo.cell_count)

        for index, screen in enumerate(screens):
            cell = Frame(geo.cell_width, geo.cell_height)
            try:
                screen.draw(cell, elapsed)
            except Exception as exc:
                if self.ctx.config.get("debug"):
                    print("cell {} failed: {}".format(index, exc))
                cell = Frame(geo.cell_width, geo.cell_height)
            panel.paste(cell.image, geo.cell_origin(index))

        # Seam hairlines, off by default.
        #
        # The intent was to stop two games reading as one wide smear of numbers.
        # On a real chained pair it does the opposite: the physical gap between
        # two panels already separates them, so the drawn line lands a pixel
        # inside the left panel and reads as a lit bar down its edge -- it looks
        # like a fault in the panel, not like a divider.
        if self.ctx.config.get("display.cell_divider", False):
            for col in range(1, geo.cols):
                panel.vline(col * geo.cell_width - 1, fill=(24, 24, 24))
            for row in range(1, geo.rows):
                panel.hline(row * geo.cell_height - 1, fill=(24, 24, 24))
        return panel

    def _draw_status(self, frame):
        """A single corner pixel when data has gone stale.

        Deliberately tiny. The panel should not turn into an error console
        because ESPN blipped, but it should be possible to tell at a glance
        that the score on screen is not current.
        """
        store = self.ctx.store
        if not store.online or store.stale_seconds() > 180:
            frame.pixel(frame.width - 1, frame.height - 1, layout.RED)

    def _draw_error(self, exc):
        """Show a render error on the panel, and never become the error itself.

        This used to draw straight onto the panel with no guard. When the cause
        was something drawing itself could not survive -- fonts unreadable after
        the privilege drop, say -- this raised its own exception on top, and the
        traceback in the journal described the error reporter rather than the
        fault. The original cause was invisible.

        So the real error is always written to stderr first, where systemd will
        keep it, and the on-panel version is strictly best effort.
        """
        print("render error: {}: {}".format(type(exc).__name__, exc),
              file=sys.stderr, flush=True)
        if self.ctx.config.get("debug"):
            traceback.print_exc(file=sys.stderr)

        try:
            frame = Frame(self.ctx.width, self.ctx.height)
            frame.text_centered(8, "ERROR", FONTS.small, layout.RED)
            frame.text_fitted((1, 18), str(exc)[:40].upper(), FONTS.tiny,
                              layout.DIM, frame.width - 2)
            self.push(frame)
        except Exception as inner:
            # Cannot even draw. Say why once, plainly, and keep the original
            # error as the headline rather than replacing it with this one.
            print("could not draw the error screen either: {}: {}".format(
                type(inner).__name__, inner), file=sys.stderr, flush=True)
            if isinstance(inner, OSError) and "cannot open resource" in str(inner):
                print("hint: the fonts are unreadable. The matrix library drops "
                      "privileges to 'daemon' after claiming GPIO, so every "
                      "directory on the way to this project needs the execute "
                      "bit for it. Run: ./scoreboard doctor",
                      file=sys.stderr, flush=True)
        time.sleep(1.0)

    def push(self, frame):
        LATEST["image"] = frame.image
        LATEST["key"] = self._current_key
        LATEST["at"] = time.time()
        self.canvas.SetImage(frame.image, 0, 0)
        self.canvas = self.matrix.SwapOnVSync(self.canvas)

    # ---------------------------------------------------------------- manual

    def skip(self):
        """Jump to the next screen now (used by the web UI)."""
        nxt = self.playlist.advance()
        self._current_key = nxt.key
        self._screen_started = time.time()


class CaptureMatrix:
    """Headless stand-in used by the screenshot tool and tests."""

    def __init__(self, width=64, height=32):
        self.width = width
        self.height = height
        self.brightness = 100
        self.frames = []

    def CreateFrameCanvas(self):
        return CaptureCanvas(self)

    def SwapOnVSync(self, canvas):
        if canvas.image is not None:
            self.frames.append(canvas.image.copy())
        return CaptureCanvas(self)


class CaptureCanvas:
    def __init__(self, matrix):
        self.matrix = matrix
        self.image = None

    def SetImage(self, image, x=0, y=0):
        self.image = image

    def Clear(self):
        self.image = Image.new("RGB", (self.matrix.width, self.matrix.height))
