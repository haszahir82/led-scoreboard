#!/usr/bin/env python3
"""LED sports scoreboard.

    python3 main.py                       # auto-detect hardware, else emulator
    python3 main.py --backend emulator    # force the desktop emulator
    python3 main.py --no-web              # skip the config web server

Enhanced fork of mikemountain/nfl-led-scoreboard: multi-league data, priority
rotation, stats screens and a phone-friendly config UI.
"""

import argparse
import signal
import sys
import threading

import os

from data import hardware, identity
from data.config import Config
from data.store import DataStore
from renderer import geometry
from renderer.display import Display, RenderContext, load_matrix
from renderer.playlist import Playlist

VERSION = "2.29.1"


def parse_args():
    parser = argparse.ArgumentParser(description="LED sports scoreboard")

    # rpi-rgb-led-matrix passthrough. Defaults are None so that "not passed"
    # is distinguishable from "passed the same value as the config": anything
    # left unset falls through to config.json, and anything given on the
    # command line wins. That is what lets the web UI own the hardware
    # settings while a flag still overrides for a one-off test.
    parser.add_argument("--led-rows", type=int, default=None)
    parser.add_argument("--led-cols", type=int, default=None)
    parser.add_argument("--led-chain", type=int, default=None)
    parser.add_argument("--led-parallel", type=int, default=None)
    parser.add_argument("--led-pwm-bits", type=int, default=None)
    parser.add_argument("--led-brightness", type=int, default=None)
    parser.add_argument("--led-gpio-mapping", default=None,
                        choices=["regular", "adafruit-hat", "adafruit-hat-pwm"])
    parser.add_argument("--led-scan-mode", type=int, default=None, choices=[0, 1])
    parser.add_argument("--led-pwm-lsb-nanoseconds", type=int, default=None)
    parser.add_argument("--led-slowdown-gpio", type=int, default=None)
    parser.add_argument("--led-no-hardware-pulse", action="store_true")
    parser.add_argument("--led-rgb-sequence", default=None)
    parser.add_argument("--led-pixel-mapper", default=None)
    parser.add_argument("--led-row-addr-type", type=int, default=None)
    parser.add_argument("--led-multiplexing", type=int, default=None)
    parser.add_argument("--led-limit-refresh", type=int, default=None)

    parser.add_argument("--backend", default="auto",
                        choices=["auto", "hardware", "emulator"])
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--no-web", action="store_true")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


# Matrix options that a board profile has an opinion about. Kept short and
# explicit: hardware.PATHS also carries frame rate and refresh cadence, which
# are not RGBMatrixOptions fields and must not be passed to the library.
PROFILE_BACKED = ("pwm_bits", "gpio_slowdown")

# What the matrix library will accept. It documents 0 to 2, and
# options-initialize.cc refuses anything above 4 with "outside usable range",
# which means the process exits instead of the panel lighting up.
SLOWDOWN_DOCUMENTED = 2
SLOWDOWN_MAX = 4


def hardware_overrides(args):
    """CLI flags that were actually supplied, keyed as the config names them."""
    mapping = {
        "rows": args.led_rows,
        "cols": args.led_cols,
        "chain_length": args.led_chain,
        "parallel": args.led_parallel,
        "pwm_bits": args.led_pwm_bits,
        "brightness": args.led_brightness,
        "gpio_mapping": args.led_gpio_mapping,
        "scan_mode": args.led_scan_mode,
        "pwm_lsb_nanoseconds": args.led_pwm_lsb_nanoseconds,
        "gpio_slowdown": args.led_slowdown_gpio,
        "rgb_sequence": args.led_rgb_sequence,
        "pixel_mapper": args.led_pixel_mapper,
        "row_address_type": args.led_row_addr_type,
        "multiplexing": args.led_multiplexing,
        "limit_refresh_hz": args.led_limit_refresh,
    }
    out = {k: v for k, v in mapping.items() if v is not None}
    if args.led_no_hardware_pulse:
        out["no_hardware_pulse"] = True
    return out


def matrix_options(config, overrides):
    """Merge board profile, config.matrix and CLI overrides into kwargs.

    Precedence is CLI flag, then config.json, then the detected board's
    profile, then the fallback here -- the order data/hardware.py documents.

    The profile step was missing, which is worth spelling out because the
    symptom was invisible: data/hardware.py sets a colour depth and a GPIO
    slowdown per board, /api/hardware reported those values, the web page
    displayed them, and nothing ever passed them to the library. A Zero W was
    advertised as running at depth 8 and slowdown 0 -- the settings that stop
    it flickering -- while actually running the library's defaults of 11 and 1.
    Every other profile value (frame rate, refresh cadence, prefetch) went
    through hardware.tuned() and worked; only the two that reach the matrix
    did not.
    """
    def value(key, default=None):
        if key in overrides:
            return overrides[key]
        got = config.get("matrix.{}".format(key))
        if got is not None and got != "":
            return got
        if key in PROFILE_BACKED:
            tuned = hardware.tuned(config, key)
            if tuned is not None:
                return tuned
        return default

    brightness = value("brightness")
    if brightness is None:
        brightness = int(config.get("display.brightness", 60))

    options = {
        "rows": value("rows", 32),
        "cols": value("cols", 64),
        "chain_length": value("chain_length", 1),
        "parallel": value("parallel", 1),
        "pwm_bits": value("pwm_bits", 11),
        "brightness": int(brightness),
        "hardware_mapping": value("gpio_mapping", "adafruit-hat"),
        "scan_mode": value("scan_mode", 1),
        "pwm_lsb_nanoseconds": value("pwm_lsb_nanoseconds", 130),
        "led_rgb_sequence": value("rgb_sequence", "RGB"),
        "pixel_mapper_config": value("pixel_mapper", ""),
        "row_address_type": value("row_address_type", 0),
        "multiplexing": value("multiplexing", 0),
    }

    # These three are only set when meaningful; passing them unconditionally
    # overrides sane library defaults with worse ones.
    slowdown = value("gpio_slowdown")
    if slowdown is not None:
        options["gpio_slowdown"] = int(slowdown)
    if value("no_hardware_pulse"):
        options["disable_hardware_pulsing"] = True
    limit = value("limit_refresh_hz", 0)
    if limit:
        options["limit_refresh_rate_hz"] = int(limit)

    return _sanity_check(options)


def _sanity_check(options):
    """Correct settings the hardware cannot honour, loudly, before starting.

    The matrix library validates some combinations by calling abort(), which
    kills the process with SIGABRT. systemd restarts it, it aborts again, and
    the board sits in a restart loop showing nothing. The reason is printed to
    stderr, so it lands in the journal where nobody looks first, and from the
    outside it is indistinguishable from a dead panel.

    A board that is a gift cannot be bricked by a wrong number in a settings
    form. So rather than letting the library abort, correct what is impossible,
    say so on stdout where the installer and `./scoreboard logs` both show it,
    and run.
    """
    mapping = str(options.get("hardware_mapping", ""))
    parallel = int(options.get("parallel", 1) or 1)

    # The Adafruit HAT and Bonnet break out a single HUB75 connector. Parallel
    # chains need one physical output per chain, driven from separate GPIO
    # banks, so on this hardware there is nowhere for a second chain to go.
    # "Chain" (panels daisy-chained off one output) is the setting people mean
    # when they have two panels; "parallel" is the one that aborts.
    if mapping.startswith("adafruit-hat") and parallel > 1:
        print("matrix: parallel={} is impossible on {} -- that board has one "
              "HUB75 output. Using parallel=1.".format(parallel, mapping),
              file=sys.stderr, flush=True)
        print("        If you have two panels wired one after the other, the "
              "setting you want is chain_length, not parallel.",
              file=sys.stderr, flush=True)
        options["parallel"] = 1

    if parallel > 3:
        print("matrix: parallel={} is above the library's maximum of 3. "
              "Using 3.".format(parallel), file=sys.stderr, flush=True)
        options["parallel"] = 3

    chain = int(options.get("chain_length", 1) or 1)
    if chain < 1:
        options["chain_length"] = 1

    # GPIO slowdown is the setting most likely to be turned the wrong way,
    # because the name suggests that more of it is safer. It is not. It pads
    # every GPIO write, so it stretches the whole refresh cycle: the panel
    # spends longer on each row, the frame rate falls, and past a point the
    # display stops looking dim and starts looking scrambled, like an
    # untuned analogue channel. Someone chasing flicker raises it, the
    # picture gets dramatically worse, and nothing about the symptom points
    # back at the setting.
    #
    # The library documents 0 to 2 and rejects anything above 4 outright,
    # which kills the process. Default is 1.
    if "gpio_slowdown" in options:
        slow = int(options["gpio_slowdown"])
        if slow > SLOWDOWN_MAX:
            print("matrix: gpio_slowdown={} is above the library's hard limit "
                  "of {}, which makes it refuse to start. Using {}."
                  .format(slow, SLOWDOWN_MAX, SLOWDOWN_MAX),
                  file=sys.stderr, flush=True)
            options["gpio_slowdown"] = slow = SLOWDOWN_MAX
        if slow > SLOWDOWN_DOCUMENTED:
            print("matrix: gpio_slowdown={} is outside the range the library "
                  "documents (0 to {}). Running it anyway."
                  .format(slow, SLOWDOWN_DOCUMENTED),
                  file=sys.stderr, flush=True)
            print("        If the panel looks scrambled or the brightness "
                  "jumps around, this is the first thing to put back. It is "
                  "not a flicker fix -- it lowers the refresh rate, which is "
                  "what causes flicker. Clear the field to let the board "
                  "choose.", file=sys.stderr, flush=True)

    return options


def start_web(config, store, port):
    from web.app import create_app
    app = create_app(config, store)
    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, threaded=True,
                               debug=False, use_reloader=False),
        daemon=True, name="web")
    thread.start()
    return thread


def main():
    args = parse_args()
    config = Config(args.config) if args.config else Config()
    if args.debug:
        config.set("debug", True)

    # Apply any queued rename now, while still root. The matrix library drops
    # privileges to `daemon` a moment from now and hostnamectl stops working,
    # so this is the one reliable window for it.
    if os.geteuid() == 0:
        applied = identity.apply_pending()
        if applied:
            print("hostname set to {}".format(applied))

    print("LED Scoreboard v{} \"{}\" - {} - leagues: {}".format(
        VERSION, config.get("identity.name", "Scoreboard"),
        identity.mdns_name(),
        ", ".join(config.enabled_leagues()) or "none"))

    board = hardware.board_for(config)
    print("board: {}".format(board.describe()))

    store = DataStore(config)
    store.start()

    if not args.no_web and config.get("web.enabled", True):
        port = args.port or int(config.get("web.port", 8080))
        try:
            start_web(config, store, port)
            print("Config UI on http://<pi-address>:{}".format(port))
        except Exception as exc:
            print("web UI failed to start: {}".format(exc))

    overrides = hardware_overrides(args)

    # Build the options once and hand the same object to both the library and
    # the geometry. They used to be derived separately from config, which meant
    # a setting the sanity check corrected for the library was still believed by
    # the renderer, and the two disagreed about how big the panel was.
    options = matrix_options(config, overrides)
    options["tile_screens"] = config.get("matrix.tile_screens", True)
    matrix, backend = load_matrix(options, args.backend)

    geo = geometry.from_options(options)
    print("matrix backend: {} | {}".format(backend, geo.describe()))

    # A screen is written for one 64x32 cell, so that is the context it gets.
    # The display tiles as many cells as the panel has room for.
    ctx = RenderContext(config, store,
                        width=geo.cell_width, height=geo.cell_height,
                        geometry=geo)
    display = Display(matrix, ctx, Playlist(ctx), geometry=geo)

    def shutdown(signum, frame):
        store.stop()
        try:
            matrix.Clear()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    display.run()


if __name__ == "__main__":
    main()
