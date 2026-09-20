"""US ZIP code to coordinates.

Weather needs latitude and longitude, but nobody knows their own to four
decimal places. A ZIP code is the thing a person can actually type, so that is
what the config stores; the coordinates are a derived, cached value.

The lookup is cached to disk because these boards go to houses where the wifi
may be flaky, and a ZIP code's coordinates do not change. Once resolved, the
weather screen keeps working forever without ever geocoding again.
"""

import json
import os
import re
import threading
from dataclasses import dataclass, asdict

import requests

CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "geocode_cache.json")

PRIMARY = "https://api.zippopotam.us/us/{zip}"
FALLBACK = ("https://geocoding-api.open-meteo.com/v1/search"
            "?name={zip}&country=US&count=1")

TIMEOUT = 8
_lock = threading.Lock()
_memory = {}


class GeocodeError(Exception):
    pass


@dataclass
class Location:
    zip: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    city: str = ""
    state: str = ""

    @property
    def label(self):
        if self.city and self.state:
            return "{}, {}".format(self.city, self.state)
        return self.city or self.zip


def valid_zip(value):
    """True for a 5-digit US ZIP. ZIP+4 is accepted and truncated."""
    return bool(re.match(r"^\d{5}(-\d{4})?$", str(value or "").strip()))


def normalize(value):
    return str(value or "").strip()[:5]


# ---------------------------------------------------------------- cache

def _load_cache():
    try:
        with open(CACHE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cache, fh, indent=2)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        # A read-only or unwritable state dir is not worth failing over; the
        # in-memory cache still covers the life of the process.
        pass


# ---------------------------------------------------------------- lookup

def _from_zippopotam(payload, zip_code):
    places = payload.get("places") or []
    if not places:
        raise GeocodeError("no places for {}".format(zip_code))
    place = places[0]
    # Note the spaces in these key names; that is genuinely the API's schema.
    return Location(
        zip=zip_code,
        latitude=float(place["latitude"]),
        longitude=float(place["longitude"]),
        city=place.get("place name", ""),
        state=place.get("state abbreviation", "") or place.get("state", ""),
    )


def _from_open_meteo(payload, zip_code):
    results = payload.get("results") or []
    if not results:
        raise GeocodeError("no results for {}".format(zip_code))
    first = results[0]
    return Location(
        zip=zip_code,
        latitude=float(first["latitude"]),
        longitude=float(first["longitude"]),
        city=first.get("name", ""),
        state=first.get("admin1", ""),
    )


def lookup(zip_code, use_cache=True):
    """ZIP -> Location. Raises GeocodeError if it cannot be resolved."""
    zip_code = normalize(zip_code)
    if not valid_zip(zip_code):
        raise GeocodeError("{} is not a 5-digit ZIP code".format(zip_code))

    if use_cache:
        with _lock:
            if zip_code in _memory:
                return Location(**_memory[zip_code])
        cache = _load_cache()
        if zip_code in cache:
            location = Location(**cache[zip_code])
            with _lock:
                _memory[zip_code] = asdict(location)
            return location

    errors = []
    for url, parse in ((PRIMARY, _from_zippopotam), (FALLBACK, _from_open_meteo)):
        try:
            resp = requests.get(url.format(zip=zip_code), timeout=TIMEOUT,
                                headers={"User-Agent": "led-scoreboard/2.1"})
            if resp.status_code == 404:
                errors.append("{} not found".format(zip_code))
                continue
            resp.raise_for_status()
            location = parse(resp.json(), zip_code)
            cache = _load_cache()
            cache[zip_code] = asdict(location)
            _save_cache(cache)
            with _lock:
                _memory[zip_code] = asdict(location)
            return location
        except GeocodeError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(str(exc))

    raise GeocodeError("could not resolve {}: {}".format(
        zip_code, "; ".join(errors) or "unknown error"))


def resolve_config(config):
    """Ensure the config's ZIP has coordinates, geocoding once if needed.

    Returns a Location or None. Never raises: a failed lookup just means the
    weather screen sits out until the next attempt.
    """
    zip_code = normalize(config.get("location.zip"))
    if not valid_zip(zip_code):
        return None

    lat = config.get("location.latitude")
    lon = config.get("location.longitude")
    cached_zip = normalize(config.get("location.resolved_zip"))

    if lat and lon and cached_zip == zip_code:
        return Location(zip=zip_code, latitude=float(lat), longitude=float(lon),
                        city=config.get("location.city", ""),
                        state=config.get("location.state", ""))

    try:
        location = lookup(zip_code)
    except GeocodeError:
        return None

    config.set("location.latitude", location.latitude)
    config.set("location.longitude", location.longitude)
    config.set("location.city", location.city)
    config.set("location.state", location.state)
    config.set("location.resolved_zip", location.zip)
    try:
        config.save()
    except Exception:
        pass
    return location
