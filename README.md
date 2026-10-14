# 🔭 First Light

A stargazing guide for beginners with a new telescope or binoculars. Tell it your city and it tells
you what's in the sky tonight, which way to face, how high to look, whether the clouds will get in
the way, and how much your city's lights are washing out the sky.

"First light" is what astronomers call the first time a telescope is pointed at the sky.

## Who it's for

Someone who just got a telescope, walks outside, and has no idea where to point it. Star charts
assume you already know the sky. This agent answers the questions beginners actually ask: *Is
Saturn up tonight? Which way is it? Will it be cloudy? Why can't I see any stars from my
apartment?*

## Tools

| Tool | What it does | Data |
| --- | --- | --- |
| `get_visible_objects` | For a city and night, checks which planets, the Moon, asteroids or comets are above the horizon after dark. For each one: the visible time window, the best time, the compass direction to face, how high it gets, and whether you need eyes, binoculars or a telescope. | [NASA JPL Horizons API](https://ssd-api.jpl.nasa.gov/doc/horizons.html) (external) |
| `get_night_forecast` | Hour-by-hour sky conditions between sunset and sunrise for the next 1–14 nights: cloud cover, chance of rain, low temperature, wind, a great/okay/poor rating, and which hours are clear. | [Open-Meteo](https://open-meteo.com/) (external) |
| `get_light_pollution` | Looks up a US city or town's light pollution score from 1 (pristine) to 10 (inner city), explains what you can still see there, and finds the nearest town with a dark sky, with miles and compass direction. | `light_pollution.db`, our own database (see below) |

The agent combines them. Ask "which night this week is best for Saturn?" and it checks the
forecast and the planet's position before answering. Ask why you can't see stars in Chicago and
it tells you the Moon and planets still look great from the city, then points you to a dark sky
80 miles away.

City names are turned into coordinates with Open-Meteo's geocoding API. Every tool returns a clear
error the model can act on (an unknown city, a busy NASA server, a town name shared by several
states) instead of crashing.

## The light pollution database

`light_pollution.db` is a SQLite table of all 32,331 US places (including Puerto Rico), each with a
light pollution score. `build_db.py` builds it in one run:

1. Takes every city, town and census-designated place from the
   [US Census 2024 Gazetteer](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html).
2. Finds each place on the North America map of the
   [2025 World Atlas of Artificial Night Sky Brightness](https://djlorenz.github.io/astronomy/lp/)
   by David Lorenz, made from NOAA's VIIRS satellite images of Earth at night.
3. Reads the map color at that spot, which is one of 15 light pollution zones, and converts it to
   a 1–10 score.

Big cities use downtown coordinates from [plotly's top 1,000 US cities](https://github.com/plotly/datasets/blob/master/us-cities-top-1k.csv),
which also supplies population to pick the right city when a name repeats (there are dozens of
Springfields).

The 1–10 score is our own conversion. The atlas measures sky brightness straight overhead, and
its author asks that it not be called the Bortle scale, so we don't.

To rebuild the database:

```bash
uv run --with pillow build_db.py
```

## Try it

1. **What can I see tonight from New York with a small telescope?**
2. **Which night this week is best to see Saturn from Chicago?**
3. **I live in Los Angeles and can barely see any stars. Is my city too light polluted, and where should I go?**

Follow-ups work too, since the agent remembers the conversation: try "what about Nashville?" after
the third one.

Each tool call appears above the answer and expands to show the exact arguments and result.

## Running locally

1. A GCP project with billing and the Agent Platform (Vertex AI) API enabled.
2. `gcloud auth application-default login`
3. `uv run app.py`, then open http://localhost:8000

The model is `vertex_ai/gemini-3.5-flash-lite`. NASA Horizons and Open-Meteo need no API keys.

## Files

- `app.py`: FastAPI server, the session store and the tool-calling loop
- `tools.py`: the three tools and their descriptions for the model
- `index.html`: the chat page
- `build_db.py`: builds `light_pollution.db`
