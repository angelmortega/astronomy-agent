"""Build light_pollution.db: every US place with a light pollution score.

Run once (the app only reads the finished database):
    uv run --with pillow build_db.py

Sources:
- Places: US Census 2024 Gazetteer, every city, town and CDP with its coordinates (public domain).
- Big cities: plotly's top 1,000 US cities, for population (to pick the bigger city when a name
  repeats) and downtown coordinates. Census points for huge cities like Anchorage land far from
  downtown, out in the mountains.
- Light pollution: David Lorenz's 2025 World Atlas of Artificial Night Sky Brightness, North America
  map (https://djlorenz.github.io/astronomy/lp/). Each pixel is 1/120 of a degree and its color is a
  light pollution zone, from 0 (pristine) to 7b (inner city).
"""

import csv
import io
import re
import sqlite3
import zipfile
from pathlib import Path

import requests
from PIL import Image

GAZETTEER_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_place_national.zip"
POPULATION_URL = "https://raw.githubusercontent.com/plotly/datasets/master/us-cities-top-1k.csv"
ATLAS_URL = "https://djlorenz.github.io/astronomy/lp2025/NorthAmerica2025.png"

# The North America map covers 7N to 75N and 180W to 51W at 120 pixels per degree.
ATLAS_NORTH, ATLAS_WEST, PIXELS_PER_DEGREE = 75, -180, 120

# Atlas color -> (zone, lowest and highest light pollution index, our 1-10 score).
# The index is artificial sky brightness divided by natural sky brightness, so 1.0 means
# the city glow is as bright as the natural night sky. Zone colors are from the atlas key.
ZONES = {
    (0, 0, 0): ("0", 0.0, 0.01, 1),
    (34, 34, 34): ("1a", 0.01, 0.06, 1),
    (66, 66, 66): ("1b", 0.06, 0.11, 2),
    (20, 47, 114): ("2a", 0.11, 0.19, 2),
    (33, 84, 216): ("2b", 0.19, 0.33, 3),
    (15, 87, 20): ("3a", 0.33, 0.58, 3),
    (31, 161, 42): ("3b", 0.58, 1.00, 4),
    (110, 100, 30): ("4a", 1.00, 1.73, 5),
    (184, 166, 37): ("4b", 1.73, 3.00, 5),
    (191, 100, 30): ("5a", 3.00, 5.20, 6),
    (253, 150, 80): ("5b", 5.20, 9.00, 7),
    (251, 90, 73): ("6a", 9.00, 15.59, 8),
    (251, 153, 138): ("6b", 15.59, 27.00, 8),
    (160, 160, 160): ("7a", 27.00, 46.77, 9),
    (242, 242, 242): ("7b", 46.77, None, 10),
}

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}

# Census names end in a type: "Austin city", "Honolulu CDP", "Nashville-Davidson metropolitan
# government (balance)". People just say "Austin", so strip it.
SUFFIX = re.compile(
    r"\s+(city and borough|consolidated government|metropolitan government|metro government|"
    r"unified government|urban county|municipality|zona urbana|comunidad|borough|village|"
    r"city|town|CDP|plantation|corporation)$"
)


def clean_name(name: str) -> str:
    name = name.removesuffix(" (balance)").removeprefix("Urban ")  # "Urban Honolulu CDP"
    while SUFFIX.search(name):
        name = SUFFIX.sub("", name)
    return name.strip()


def main():
    print("Downloading the light pollution atlas (about 2.5 MB)...")
    Image.MAX_IMAGE_PIXELS = None  # the map is 15480 x 8160
    atlas = Image.open(io.BytesIO(requests.get(ATLAS_URL, timeout=120).content)).convert("RGB")

    print("Downloading the Census list of places...")
    archive = zipfile.ZipFile(io.BytesIO(requests.get(GAZETTEER_URL, timeout=120).content))
    gazetteer = archive.read(archive.namelist()[0]).decode("utf-8")

    big_cities = {}
    for row in csv.DictReader(io.StringIO(requests.get(POPULATION_URL, timeout=60).text)):
        big_cities[(row["City"].lower(), row["State"].lower())] = row

    rows = []
    for row in csv.DictReader(io.StringIO(gazetteer), delimiter="\t"):
        row = {k.strip(): v.strip() for k, v in row.items()}
        state = row["USPS"]
        if state not in STATES:
            continue
        name = clean_name(row["NAME"])
        # "Nashville-Davidson" and "Louisville/Jefferson County" are just "Nashville" and "Louisville" to plotly.
        short_name = re.split(r"[-/]", name)[0]
        big = big_cities.get((name.lower(), STATES[state].lower())) or big_cities.get((short_name.lower(), STATES[state].lower()))
        lat, lon = (float(big["lat"]), float(big["lon"])) if big else (float(row["INTPTLAT"]), float(row["INTPTLONG"]))
        x = int((lon - ATLAS_WEST) * PIXELS_PER_DEGREE)
        y = int((ATLAS_NORTH - lat) * PIXELS_PER_DEGREE)
        if not (0 <= x < atlas.width and 0 <= y < atlas.height):
            continue  # a few Aleutian islands sit west of the map
        zone, lpi_low, lpi_high, score = ZONES[atlas.getpixel((x, y))]
        rows.append((
            name, state, STATES[state], lat, lon, zone, lpi_low, lpi_high, score,
            int(big["Population"]) if big else None,
        ))

    db_path = Path(__file__).parent / "light_pollution.db"
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    db.execute("""
        CREATE TABLE places (
            name TEXT, state TEXT, state_name TEXT, lat REAL, lon REAL,
            zone TEXT, lpi_low REAL, lpi_high REAL, score INTEGER, population INTEGER
        )
    """)
    db.executemany("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    db.execute("CREATE INDEX places_name ON places (name COLLATE NOCASE)")
    db.execute("CREATE INDEX places_score ON places (score)")
    db.commit()
    db.close()
    print(f"Wrote {len(rows)} places to {db_path.name}")


if __name__ == "__main__":
    main()
