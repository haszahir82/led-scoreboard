"""Board detection and per-board tuning defaults.

The Zero W needed a frame rate of 8, a colour depth of 8, and a slow refresh
cadence. A Pi 3 A+ needs none of that, and baking the Zero's limits in as
constants would mean every board runs at the speed of the weakest one.

So the numbers that depend on the hardware are resolved at startup: the board
identifies itself, a profile supplies defaults, and anything set explicitly in
config.json wins over both. Someone who never opens the settings gets sensible
behaviour on whatever Pi they were handed; someone who wants to tune still can.

Precedence, always:  explicit config  >  board profile  >  fallback
"""

import os
import re

MODEL_PATH = "/proc/device-tree/model"
CPUINFO_PATH = "/proc/cpuinfo"


class Profile:
    """Tuning defaults for a class of board."""

    def __init__(self, key, label, frame_rate, pwm_bits, gpio_slowdown,
                 live_seconds, idle_seconds, logo_prefetch):
        self.key = key
        self.label = label
        self.frame_rate = frame_rate
        self.pwm_bits = pwm_bits
        self.gpio_slowdown = gpio_slowdown
        self.live_seconds = live_seconds
        self.idle_seconds = idle_seconds
        self.logo_prefetch = logo_prefetch

    def as_dict(self):
        return {k: v for k, v in vars(self).items()}


PROFILES = {
    # Single ARMv6 core. Everything competes for one CPU, including the
    # matrix refresh thread, so the frame rate and colour depth both come
    # down and network work is made as rare as it can be.
    "zero_w": Profile(
        "zero_w", "Pi Zero / Zero W",
        frame_rate=8, pwm_bits=8, gpio_slowdown=0,
        live_seconds=35, idle_seconds=600, logo_prefetch=6),

    # Quad-core A53. Comfortable: full colour depth and a smooth frame rate.
    "quad_small": Profile(
        "quad_small", "Pi Zero 2 W / Pi 2",
        frame_rate=20, pwm_bits=11, gpio_slowdown=1,
        live_seconds=20, idle_seconds=300, logo_prefetch=16),

    # The Pi 3 family used to share the profile above, which cost it a third of
    # its frame rate for no reason: a 3 A+ clocks 1.4 GHz against the Zero 2 W's
    # 1.0 GHz on the same Cortex-A53. Colour depth stays at 11 because that is
    # the library's ceiling, not a conservative choice. Logo prefetch does NOT
    # go up with the clock: a 3 A+ has the same 512 MB as a Zero 2 W, and the
    # cache is bounded by memory rather than speed.
    "pi3": Profile(
        "pi3", "Pi 3 / 3 A+ / 3 B+",
        frame_rate=30, pwm_bits=11, gpio_slowdown=1,
        live_seconds=15, idle_seconds=300, logo_prefetch=16),

    # Faster GPIO than the panel can follow, hence the higher slowdown.
    "pi4": Profile(
        "pi4", "Pi 4",
        frame_rate=30, pwm_bits=11, gpio_slowdown=2,
        live_seconds=15, idle_seconds=300, logo_prefetch=24),

    # RP1 GPIO. The library's Pi 5 support is newer than the rest, so this
    # stays conservative on slowdown rather than assuming the best case.
    "pi5": Profile(
        "pi5", "Pi 5",
        frame_rate=30, pwm_bits=11, gpio_slowdown=2,
        live_seconds=15, idle_seconds=300, logo_prefetch=24),

    # Laptop, emulator, or a Pi we do not recognise. Middle of the road.
    "generic": Profile(
        "generic", "Unrecognised board",
        frame_rate=20, pwm_bits=11, gpio_slowdown=None,
        live_seconds=20, idle_seconds=300, logo_prefetch=16),
}

DEFAULT_PROFILE = "generic"


class Board:
    def __init__(self, model="", profile_key=DEFAULT_PROFILE, cores=0, detected=False):
        self.model = model
        self.profile_key = profile_key
        self.cores = cores
        self.detected = detected

    @property
    def profile(self):
        return PROFILES.get(self.profile_key, PROFILES[DEFAULT_PROFILE])

    def describe(self):
        if not self.detected:
            return "board not detected, using {} profile".format(
                self.profile.label.lower())
        return "{} -> {} profile".format(self.model, self.profile.label)


def _read_model():
    try:
        with open(MODEL_PATH, "rb") as fh:
            # The device-tree string is NUL-terminated.
            return fh.read().decode("utf-8", "replace").strip("\x00").strip()
    except Exception:
        return ""


def _read_cores():
    try:
        with open(CPUINFO_PATH) as fh:
            return len(re.findall(r"^processor\s*:", fh.read(), re.M))
    except Exception:
        return 0


def classify(model, cores=0):
    """Model string -> profile key.

    Ordered most specific first. 'Zero 2' has to be tested before 'Zero',
    since the Zero 2 W's model string contains both and they need opposite
    treatment: one is a single ARMv6 core, the other is quad-core ARMv8.
    """
    text = (model or "").lower()

    if "raspberry pi 5" in text or "pi 5 " in text:
        return "pi5"
    if "raspberry pi 4" in text or "compute module 4" in text:
        return "pi4"
    if "zero 2" in text:
        return "quad_small"
    if "zero" in text:
        return "zero_w"
    if "raspberry pi 3" in text or "compute module 3" in text:
        return "pi3"
    if "raspberry pi 2" in text:
        return "quad_small"
    if "raspberry pi model" in text or "raspberry pi 1" in text:
        return "zero_w"          # original single-core Pi

    if text:
        # An unknown Raspberry Pi: guess from core count rather than give up.
        return "quad_small" if cores >= 4 else "zero_w"
    return DEFAULT_PROFILE


_cached = None


def detect(force=False):
    global _cached
    if _cached is not None and not force:
        return _cached
    model = _read_model()
    cores = _read_cores()
    _cached = Board(model=model, profile_key=classify(model, cores),
                    cores=cores, detected=bool(model))
    return _cached


def board_for(config):
    """Detected board, unless the config pins a profile explicitly."""
    forced = str(config.get("performance.profile", "auto") or "auto").lower()
    if forced != "auto" and forced in PROFILES:
        board = detect()
        return Board(model=board.model or "forced", profile_key=forced,
                     cores=board.cores, detected=board.detected)
    return detect()


def resolve(config, name, config_path=None):
    """Tuned value for `name`: explicit config first, else the board profile.

    `config_path` is where a user override lives when it is not under
    performance.* (colour depth lives under matrix, for instance).
    """
    path = config_path or "performance.{}".format(name)
    explicit = config.get(path)
    if explicit is not None and explicit != "":
        return explicit
    return getattr(board_for(config).profile, name, None)


# Where each tunable's user override lives, so callers do not have to know.
# max_games is deliberately absent: how many games you want in the loop is a
# preference, not a board limit, and the costs that used to make it one (logo
# fetches, refresh cadence) are handled directly above.
PATHS = {
    "frame_rate": "performance.frame_rate",
    "logo_prefetch": "performance.logo_prefetch",
    "pwm_bits": "matrix.pwm_bits",
    "gpio_slowdown": "matrix.gpio_slowdown",
    "live_seconds": "refresh.live_seconds",
    "idle_seconds": "refresh.idle_seconds",
}


def tuned(config, name):
    return resolve(config, name, PATHS.get(name))


def summary(config):
    board = board_for(config)
    return {
        "model": board.model,
        "detected": board.detected,
        "cores": board.cores,
        "profile": board.profile.key,
        "profile_label": board.profile.label,
        "values": {name: tuned(config, name) for name in PATHS},
    }
