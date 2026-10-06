"""The tools the harness can run, and the JSON that describes them to the model."""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# Open-Meteo is free and needs no API key.
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# NASA JPL Horizons is free and needs no API key.
HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"

# Horizons IDs for the bodies a beginner cares about most.
MAJOR_BODIES = {
    "moon": "301",
    "mercury": "199",
    "venus": "299",
    "mars": "499",
    "jupiter": "599",
    "saturn": "699",
    "uranus": "799",
    "neptune": "899",
}
DEFAULT_OBJECTS = ["Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune"]

# Below this altitude, trees and buildings usually block the view.
MIN_ALTITUDE_DEG = 10

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def geocode(location: str) -> dict:
    """Turn a city name into name, latitude, longitude and timezone. Raises LookupError if not found.

    Open-Meteo searches by city name only, so "Paris, Texas" searches "Paris" and then
    prefers the match whose state, country or country code contains "Texas".
    """
    city, _, region = (part.strip() for part in location.partition(","))
    places = requests.get(GEOCODE_URL, params={"name": city, "count": 10}, timeout=10).json()
    if not places.get("results"):
        raise LookupError(f"City '{location}' was not found. Try a larger nearby city or check the spelling.")

    place = places["results"][0]  # Open-Meteo ranks bigger cities first
    if region:
        for candidate in places["results"]:
            fields = [candidate.get(k) or "" for k in ("admin1", "country", "country_code")]
            if any(region.lower() in f.lower() for f in fields):
                place = candidate
                break

    country = place.get("country") or place.get("country_code", "")
    return {
        "name": f"{place['name']}, {country}".strip(", "),
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "timezone": place.get("timezone", "UTC"),
    }


def get_weather(location: str) -> str:
    """Get the current weather for a location."""
    try:
        place = geocode(location)
        current = requests.get(
            FORECAST_URL,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
            },
            timeout=10,
        ).json()["current"]
    except LookupError as e:
        return json.dumps({"error": str(e)})
    except requests.RequestException as e:
        # The model cannot see an exception. Return something it can reason about.
        return json.dumps({"error": f"Weather service failed: {e}"})

    return json.dumps({
        "location": place["name"],
        "temp_f": current["temperature_2m"],
        "humidity": current["relative_humidity_2m"],
        "wind_mph": current["wind_speed_10m"],
    })


def _horizons_command(name: str) -> str:
    """Horizons wants an ID for planets, 'NAME;' for asteroids and 'DES=1P;CAP;' for periodic comets."""
    key = name.strip().lower()
    if key in MAJOR_BODIES:
        return MAJOR_BODIES[key]
    if re.fullmatch(r"\d+p", key):
        return f"DES={key.upper()};CAP;"
    return f"{name.strip()};"


def _how_to_see(magnitude: float | None) -> str:
    """Lower magnitude is brighter. These cut-offs assume a dark-ish sky."""
    if magnitude is None:
        return "unknown brightness"
    if magnitude <= 5:
        return "naked eye"
    if magnitude <= 9:
        return "binoculars"
    if magnitude <= 13:
        return "small telescope"
    return "too faint for beginner gear"


def _track_object(name: str, place: dict, start_utc: datetime, stop_utc: datetime) -> dict:
    """Ask Horizons where one object is every 30 minutes tonight, and summarize when it is up in the dark."""
    params = {
        "format": "json",
        "COMMAND": f"'{_horizons_command(name)}'",
        "EPHEM_TYPE": "OBSERVER",
        "CENTER": "'coord@399'",
        "COORD_TYPE": "GEODETIC",
        "SITE_COORD": f"'{place['longitude']},{place['latitude']},0'",
        "START_TIME": f"'{start_utc:%Y-%m-%d %H:%M}'",
        "STOP_TIME": f"'{stop_utc:%Y-%m-%d %H:%M}'",
        "STEP_SIZE": "'30m'",
        "QUANTITIES": "'4,9'",  # 4 = azimuth/altitude, 9 = brightness
        "CSV_FORMAT": "YES",
    }
    # Horizons answers 503 when it is busy. One retry after a short pause usually gets through.
    response = requests.get(HORIZONS_URL, params=params, timeout=30)
    if response.status_code == 503:
        time.sleep(2)
        response = requests.get(HORIZONS_URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    text = data.get("result", "")
    if "$$SOE" not in text:
        # Usually an unknown name or several matches. Pass Horizons' own explanation along.
        hint = (data.get("error") or text).strip()[:400]
        return {"object": name, "error": f"Horizons could not track '{name}'. {hint}"}

    tz = ZoneInfo(place["timezone"])
    dark_rows = []
    for line in text.split("$$SOE")[1].split("$$EOE")[0].strip().splitlines():
        when, sun, _moon, azimuth, altitude, magnitude, *_ = [c.strip() for c in line.split(",")]
        # Sun flag: '*' is daylight, 'C' is civil twilight. Anything else is dark enough.
        if sun in ("*", "C"):
            continue
        local = datetime.strptime(when, "%Y-%b-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(tz)
        dark_rows.append({
            "time": local,
            "azimuth": float(azimuth),
            "altitude": float(altitude),
            "magnitude": None if magnitude == "n.a." else float(magnitude),
        })

    up = [r for r in dark_rows if r["altitude"] >= MIN_ALTITUDE_DEG]
    magnitude = dark_rows[0]["magnitude"] if dark_rows else None
    if not up:
        return {"object": name, "visible_tonight": False, "magnitude": magnitude,
                "reason": f"Never more than {MIN_ALTITUDE_DEG} degrees above the horizon while it is dark."}

    best = max(up, key=lambda r: r["altitude"])
    return {
        "object": name,
        "visible_tonight": True,
        "visible_from": f"{up[0]['time']:%-I:%M %p}",
        "visible_until": f"{up[-1]['time']:%-I:%M %p}",
        "best_time": f"{best['time']:%-I:%M %p}",
        "altitude_deg": round(best["altitude"]),
        "direction": COMPASS[round(best["azimuth"] / 22.5) % 16],
        "azimuth_deg": round(best["azimuth"]),
        "magnitude": magnitude,
        "how_to_see": _how_to_see(magnitude),
    }


def get_visible_objects(location: str, objects: list[str] | None = None, night: str | None = None) -> str:
    """Which objects are above the horizon after dark, when, and which way to point."""
    try:
        place = geocode(location)
    except LookupError as e:
        return json.dumps({"error": str(e)})
    except requests.RequestException as e:
        return json.dumps({"error": f"Geocoding service failed: {e}"})

    tz = ZoneInfo(place["timezone"])
    try:
        evening = date.fromisoformat(night) if night else datetime.now(tz).date()
    except ValueError:
        return json.dumps({"error": f"Night '{night}' is not a date. Use YYYY-MM-DD, or leave it out for tonight."})

    # 6pm to 6am local covers the whole night; Horizons' sun flag trims it to the dark part.
    start = datetime.combine(evening, datetime.min.time(), tz) + timedelta(hours=18)
    stop = start + timedelta(hours=12)
    start_utc, stop_utc = start.astimezone(timezone.utc), stop.astimezone(timezone.utc)

    names = objects or DEFAULT_OBJECTS

    def track(name: str) -> dict:
        try:
            return _track_object(name, place, start_utc, stop_utc)
        except requests.RequestException as e:
            return {"object": name, "error": f"Horizons request failed: {e}. Try again in a moment."}

    # One Horizons request per object. NASA turns away more than about two at a time.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(track, names))

    return json.dumps({
        "location": place["name"],
        "night_of": evening.isoformat(),
        "objects": results,
    })


# What the model sees: the "set notes" in the screenplay.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather (temperature, humidity, wind) for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name, e.g. 'New York'"},
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_visible_objects",
            "description": (
                "Find which solar system objects are above the horizon after dark from a city, using NASA JPL "
                "Horizons. For each object returns whether it is visible that night, the visible time window, "
                "the best time, how high it gets (degrees), the compass direction to point, its brightness "
                "(magnitude, lower is brighter) and whether it needs naked eye, binoculars or a telescope. "
                "Covers the Moon, planets, asteroids and comets. It does not cover stars, nebulae or galaxies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name, e.g. 'San Juan, Puerto Rico'"},
                    "objects": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Objects to check. Leave out to check the Moon and all planets. Planets and the Moon "
                            "by name ('Saturn'), asteroids by name ('Ceres', 'Vesta'), periodic comets by "
                            "designation ('1P' for Halley), other comets by designation ('C/2023 A3')."
                        ),
                    },
                    "night": {
                        "type": "string",
                        "description": "Evening to check as YYYY-MM-DD, in the city's local time. Leave out for tonight.",
                    },
                },
                "required": ["location"],
            },
        },
    },
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {"get_weather": get_weather, "get_visible_objects": get_visible_objects}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})
