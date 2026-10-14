"""The tools the harness can run, and the JSON that describes them to the model."""

import json
import math
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Open-Meteo is free and needs no API key.
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# NASA JPL Horizons is free and needs no API key.
HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"

# Every US place with a light pollution score, built by build_db.py.
LIGHT_POLLUTION_DB = Path(__file__).parent / "light_pollution.db"

# Places this dark or darker show the Milky Way clearly.
DARK_SKY_SCORE = 4

# What a beginner can expect to see at each score.
SKY_BY_SCORE = {
    1: "Pristine dark sky. The Milky Way is bright enough to cast detail, and thousands of stars are visible.",
    2: "Excellent dark sky. The Milky Way is bright and detailed.",
    3: "Rural sky. The Milky Way is clearly visible.",
    4: "Rural sky with some glow on the horizon. The Milky Way is clearly visible overhead.",
    5: "Rural to suburban. City glow is about as bright as the natural sky; the Milky Way is visible but washed out low down.",
    6: "Suburban sky. The Milky Way is faint, only overhead on the best nights.",
    7: "Bright suburban sky. The Milky Way is hard to see. Bright star clusters still work.",
    8: "Urban sky. No Milky Way. Stick to the Moon, planets, bright stars and bright clusters.",
    9: "City sky. Only the brightest stars. The Moon and planets still look great; faint galaxies and nebulae are washed out.",
    10: "Inner-city sky, among the brightest in the country. The Moon and planets are the targets here.",
}

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


def _rate_hour(clouds: float, rain_chance: float, wind_mph: float) -> str:
    """How good one hour is for a beginner with a small telescope."""
    if rain_chance >= 40 or clouds > 70:
        return "poor"
    if clouds <= 25 and rain_chance < 20 and wind_mph < 15:
        return "great"
    return "okay"


def get_night_forecast(location: str, nights: int = 7) -> str:
    """Hour-by-hour sky conditions between sunset and sunrise for the next few nights."""
    nights = max(1, min(nights, 14))
    try:
        place = geocode(location)
        forecast = requests.get(
            FORECAST_URL,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "hourly": "cloud_cover,precipitation_probability,temperature_2m,wind_speed_10m",
                "daily": "sunrise,sunset",
                "timezone": "auto",
                "forecast_days": nights + 1,  # the last night ends on the morning after
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
            },
            timeout=10,
        ).json()
        hourly, daily = forecast["hourly"], forecast["daily"]
    except LookupError as e:
        return json.dumps({"error": str(e)})
    except (requests.RequestException, KeyError) as e:
        # The model cannot see an exception. Return something it can reason about.
        return json.dumps({"error": f"Weather service failed: {e}. Try again in a moment."})

    hours = [
        {
            "time": datetime.fromisoformat(t),
            "clouds": hourly["cloud_cover"][i],
            "rain_chance": hourly["precipitation_probability"][i] or 0,
            "temp_f": hourly["temperature_2m"][i],
            "wind_mph": hourly["wind_speed_10m"][i],
        }
        for i, t in enumerate(hourly["time"])
    ]

    results = []
    for day in range(nights):
        # Dark enough for planets starts about an hour after sunset and ends an hour before sunrise.
        dark_start = datetime.fromisoformat(daily["sunset"][day]) + timedelta(hours=1)
        dark_end = datetime.fromisoformat(daily["sunrise"][day + 1]) - timedelta(hours=1)
        night = [h for h in hours if dark_start <= h["time"] <= dark_end and h["clouds"] is not None]
        if not night:
            continue

        for h in night:
            h["rating"] = _rate_hour(h["clouds"], h["rain_chance"], h["wind_mph"])
        good = [h for h in night if h["rating"] != "poor"]
        great = [h for h in night if h["rating"] == "great"]

        results.append({
            "night_of": daily["sunset"][day][:10],
            "weekday": f"{dark_start:%A}",
            "rating": "great" if len(great) >= 3 else "okay" if len(good) >= 3 else "poor",
            "dark_from": f"{dark_start:%-I:%M %p}",
            "dark_until": f"{dark_end:%-I:%M %p}",
            "avg_cloud_cover_pct": round(sum(h["clouds"] for h in night) / len(night)),
            "max_rain_chance_pct": max(h["rain_chance"] for h in night),
            "low_temp_f": round(min(h["temp_f"] for h in night)),
            "max_wind_mph": round(max(h["wind_mph"] for h in night)),
            "clear_hours": [f"{h['time']:%-I %p}" for h in good],
        })

    return json.dumps({"location": place["name"], "nights": results})


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
        "weekday": f"{evening:%A}",
        "objects": results,
    })


def _miles_and_direction(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[int, str]:
    """Straight-line miles between two points, and the compass direction from the first to the second."""
    p1, p2, dlon = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    miles = 3959 * 2 * math.asin(math.sqrt(a))
    bearing = math.degrees(math.atan2(
        math.sin(dlon) * math.cos(p2),
        math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon),
    )) % 360
    return round(miles), COMPASS[round(bearing / 22.5) % 16]


def get_light_pollution(city: str, state: str | None = None) -> str:
    """Look up a US city's light pollution score and the nearest place with a dark sky."""
    if not state and "," in city:
        city, state = (part.strip() for part in city.split(",", 1))
    city = city.strip()

    db = sqlite3.connect(LIGHT_POLLUTION_DB)
    db.row_factory = sqlite3.Row
    columns = "name, state, state_name, lat, lon, zone, lpi_low, lpi_high, score, population"
    # The exact name, plus names that start with it ("Nashville" -> "Nashville-Davidson", "Boise" -> "Boise City").
    matches = db.execute(
        f"SELECT {columns} FROM places WHERE name = ? COLLATE NOCASE OR name LIKE ? OR name LIKE ? OR name LIKE ?",
        (city, f"{city}-%", f"{city}/%", f"{city} City"),
    ).fetchall()
    if state:
        matches = [m for m in matches if state.lower() in (m["state"].lower(), m["state_name"].lower())]

    if not matches:
        db.close()
        where = f" in {state}" if state else ""
        return json.dumps({"error": (
            f"'{city}'{where} is not in the database, which covers US cities and towns, including Puerto Rico. "
            "Check the spelling, or try the nearest larger city."
        )})

    # Several places share a name (there are dozens of Springfields). Prefer a big city;
    # if none is big, ask which state.
    big = [m for m in matches if m["population"]]
    if len(matches) > 1 and not big:
        db.close()
        states = sorted({m["state_name"] for m in matches})
        return json.dumps({"error": (
            f"There are {len(matches)} places called '{city}', in: {', '.join(states[:15])}. "
            "Ask the user which state, then call again with the state."
        )})
    place = max(big, key=lambda m: m["population"]) if big else matches[0]

    result = {
        "place": f"{place['name']}, {place['state']}",
        "score": place["score"],
        "score_scale": "1 (pristine dark sky) to 10 (brightest inner city)",
        "atlas_zone": place["zone"],
        "city_glow_vs_natural_sky": (
            f"{place['lpi_low']}x to {place['lpi_high']}x" if place["lpi_high"] else f"over {place['lpi_low']}x"
        ),
        "what_you_can_see": SKY_BY_SCORE[place["score"]],
    }

    if place["score"] > DARK_SKY_SCORE:
        # Nearest dark place: search a box around the city first, then widen if it comes up empty.
        for box in (3, 8, 30):
            candidates = db.execute(
                "SELECT name, state, lat, lon, score FROM places "
                "WHERE score <= ? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                (DARK_SKY_SCORE, place["lat"] - box, place["lat"] + box, place["lon"] - box, place["lon"] + box),
            ).fetchall()
            if candidates:
                break
        if candidates:
            nearest = min(candidates, key=lambda c: _miles_and_direction(place["lat"], place["lon"], c["lat"], c["lon"])[0])
            miles, direction = _miles_and_direction(place["lat"], place["lon"], nearest["lat"], nearest["lon"])
            result["nearest_dark_sky"] = {
                "place": f"{nearest['name']}, {nearest['state']}",
                "score": nearest["score"],
                "miles_away": miles,
                "direction": direction,
                "what_you_can_see": SKY_BY_SCORE[nearest["score"]],
            }
    db.close()
    result["source"] = "2025 World Atlas of Artificial Night Sky Brightness (David Lorenz), US Census places"
    return json.dumps(result)


# What the model sees: the "set notes" in the screenplay.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_night_forecast",
            "description": (
                "Get the stargazing weather for a city from Open-Meteo: for each upcoming night, the dark "
                "hours, average cloud cover, chance of rain, low temperature, wind, an overall rating "
                "(great, okay or poor) and which hours are clear enough to observe. Use it whenever the "
                "user asks if they can go out tonight or which night this week is best."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name, e.g. 'New York' or 'Paris, Texas'"},
                    "nights": {
                        "type": "integer",
                        "description": "How many nights to forecast, starting tonight. 1 for just tonight. Default 7, max 14.",
                    },
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
    {
        "type": "function",
        "function": {
            "name": "get_light_pollution",
            "description": (
                "Look up how light polluted a US city or town is (including Puerto Rico), from a database "
                "built on the 2025 light pollution atlas. Returns a score from 1 (pristine dark sky) to 10 "
                "(brightest inner city), how many times brighter the city glow is than a natural night sky, "
                "what a beginner can expect to see there, and, if the sky is bright, the nearest town with a "
                "dark sky (miles and compass direction). Use it when the user asks how dark their sky is, "
                "why they can't see much, or where to go for a darker sky."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "US city or town name, e.g. 'Chicago' or 'Springfield'"},
                    "state": {
                        "type": "string",
                        "description": "State name or two-letter code, e.g. 'IL'. Needed when several towns share the name.",
                    },
                },
                "required": ["city"],
            },
        },
    },
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {
    "get_night_forecast": get_night_forecast,
    "get_visible_objects": get_visible_objects,
    "get_light_pollution": get_light_pollution,
}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})
