"""Current conditions from open-meteo (no account, no API key)."""

from dataclasses import dataclass

import requests

URL = ("https://api.open-meteo.com/v1/forecast"
       "?latitude={lat}&longitude={lon}"
       "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
       "&daily=temperature_2m_max,temperature_2m_min"
       "&temperature_unit={tunit}&wind_speed_unit={wunit}&timezone=auto&forecast_days=1")

# WMO weather codes collapsed to the handful of icons a 64x32 panel can show.
CODE_ICON = {
    0: "sun", 1: "sun", 2: "partly", 3: "cloud",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle", 56: "drizzle", 57: "drizzle",
    61: "rain", 63: "rain", 65: "rain", 66: "rain", 67: "rain",
    71: "snow", 73: "snow", 75: "snow", 77: "snow",
    80: "rain", 81: "rain", 82: "rain", 85: "snow", 86: "snow",
    95: "storm", 96: "storm", 99: "storm",
}

CODE_TEXT = {
    "sun": "CLEAR", "partly": "P CLOUDY", "cloud": "CLOUDY", "fog": "FOG",
    "drizzle": "DRIZZLE", "rain": "RAIN", "snow": "SNOW", "storm": "STORM",
}


@dataclass
class Weather:
    temp: int = 0
    feels: int = 0
    high: int = 0
    low: int = 0
    wind: int = 0
    icon: str = "sun"
    text: str = "CLEAR"
    unit: str = "F"


def fetch(latitude, longitude, units="imperial"):
    imperial = units == "imperial"
    url = URL.format(
        lat=latitude, lon=longitude,
        tunit="fahrenheit" if imperial else "celsius",
        wunit="mph" if imperial else "kmh")
    payload = requests.get(url, timeout=8).json()

    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    code = int(current.get("weather_code", 0))
    icon = CODE_ICON.get(code, "cloud")

    def first(key, default=0):
        values = daily.get(key) or [default]
        return values[0]

    return Weather(
        temp=round(current.get("temperature_2m", 0)),
        feels=round(current.get("apparent_temperature", 0)),
        high=round(first("temperature_2m_max")),
        low=round(first("temperature_2m_min")),
        wind=round(current.get("wind_speed_10m", 0)),
        icon=icon,
        text=CODE_TEXT.get(icon, "CLEAR"),
        unit="F" if imperial else "C",
    )
