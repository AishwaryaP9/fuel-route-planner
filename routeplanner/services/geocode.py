"""Turn a place name, ZIP code or "lat,lon" text into coordinates, offline whenever possible.

Order: "lat,lon", ZIP code, "City, ST", a city name found in one state only, and Nominatim as a last resort.
"""

import difflib
import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass

import geonamescache
import requests
import zipcodes
from django.conf import settings
from django.core.cache import cache

from .geography import in_usa, nearest_city

log = logging.getLogger(__name__)

_STATES = geonamescache.GeonamesCache().get_us_states()  # 50 states and DC
STATE_CODES = set(_STATES)
_STATE_BY_NAME = {info["name"].lower(): code for code, info in _STATES.items()}

MAX_INPUT_LENGTH = 200
TYPO_CUTOFF = 0.85
TYPO_MAX_LENGTH_DIFFERENCE = 3
TYPO_MAX_SUGGESTIONS = 3
MAX_STATES_LISTED = 10
MAX_STATES_PER_SUGGESTION = 8

_ABBREVIATIONS = ((r"^st\.? ", "saint "), (r"^ste\.? ", "sainte "), (r"^ft\.? ", "fort "), (r"^mt\.? ", "mount "))
_ALLOWED_RE = re.compile(r"[\w\s.,'#&/()\-]+")
_COORD_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_BARE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z .'\-]*")


class LocationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Location:
    query: str
    lat: float
    lon: float
    label: str
    source: str  # coordinates, zip, city or nominatim
    api_calls: int = 0


_city_index: dict[tuple[str, str], list[tuple[float, float]]] | None = None
_states_by_city: dict[str, set[str]] | None = None


def state_code(text: str) -> str | None:
    """Return the two-letter code for 'IL', 'il' or 'Illinois', or None if it is not a US state or DC."""
    cleaned = text.strip()
    return cleaned.upper() if cleaned.upper() in STATE_CODES else _STATE_BY_NAME.get(cleaned.lower())


def city_index() -> dict[tuple[str, str], list[tuple[float, float]]]:
    """Return {(city in lower case, state): [ZIP centre points]} from the offline ZIP database."""
    global _city_index
    if _city_index is None:
        index: defaultdict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
        for record in zipcodes.list_all():
            if not record["lat"] or not record["long"]:
                continue
            point = (float(record["lat"]), float(record["long"]))
            index[(record["city"].lower(), record["state"])].append(point)
            for alias in record.get("acceptable_cities") or []:
                index[(alias.lower(), record["state"])].append(point)
        _city_index = dict(index)
    return _city_index


def states_for_city(city: str) -> list[str]:
    """Return every US state that has a place with this name."""
    global _states_by_city
    if _states_by_city is None:
        by_name: defaultdict[str, set[str]] = defaultdict(set)
        for name, state in city_index():
            by_name[name].add(state)
        _states_by_city = dict(by_name)
    return sorted(_states_by_city.get(re.sub(r"\s+", " ", city.strip().lower()), ()))


def lookup_city(city: str, state: str) -> tuple[float, float] | None:
    """Return the centre of a US city (the mean of its ZIP centres), or None."""
    city = re.sub(r"\s+", " ", city.strip().lower())
    index = city_index()
    variants = [city, city.replace(" ", "")]  # "De Forest" is stored as "deforest"
    for pattern, replacement in _ABBREVIATIONS:
        if re.match(pattern, city):
            variants.append(re.sub(pattern, replacement, city))
    for variant in variants:
        points = index.get((variant, state.upper())) or index.get((variant.replace(" ", ""), state.upper()))
        if points:
            return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
    return None


def _split_city_state(text: str) -> tuple[str, str] | None:
    """Parse 'City, ST', 'City, State', 'City ST' or '..., City, ST 12345[, USA]' into (city, state code).

    A two-letter tail that is not a US state ("Toronto, ON") raises, so foreign places are rejected instead of matched.
    """
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) >= 2:
        if parts[-1].lower() in ("usa", "us", "united states") and len(parts) >= 3:
            parts = parts[:-1]
        tail = re.sub(r"\s*\d{5}(-\d{4})?$", "", parts[-1]).strip()
        state = state_code(tail)
        if state:
            return parts[-2], state
        if re.fullmatch(r"[A-Za-z]{2}", tail):
            raise LocationError(f"'{tail}' is not a US state; only locations in the USA are supported")
    else:
        match = re.match(r"^(.+?)\s+([A-Za-z]{2})$", text.strip())
        if match:
            code = state_code(match.group(2))
            if code:
                return match.group(1), code
    return None


def _resolve_offline(text: str) -> Location | None:
    """Resolve without a network call, or return None."""
    city_state = _split_city_state(text)
    if city_state:
        point = lookup_city(*city_state)
        if point:
            return Location(text, point[0], point[1], f"{city_state[0].title()}, {city_state[1]}", "city")

    if re.fullmatch(r"\d{5}(-\d{4})?", text):
        found = zipcodes.matching(text[:5])
        if not found or not found[0]["lat"]:
            raise LocationError(f"unknown US ZIP code '{text}'")
    else:
        zip_match = _ZIP_RE.search(text)
        found = zipcodes.matching(zip_match.group(1)) if zip_match else None
    if found and found[0]["lat"]:
        record = found[0]
        label = f"{record['city']}, {record['state']} {record['zip_code']}"
        return Location(text, float(record["lat"]), float(record["long"]), label, "zip")

    if _BARE_NAME_RE.fullmatch(text):
        states = states_for_city(text)
        if len(states) == 1:
            point = lookup_city(text, states[0])
            assert point is not None
            return Location(text, point[0], point[1], f"{text.strip().title()}, {states[0]}", "city")
        if len(states) > 1:
            more = "..." if len(states) > MAX_STATES_LISTED else ""
            raise LocationError(
                f"'{text}' is ambiguous - add a state, e.g. '{text}, {states[0]}' "
                f"(US states with a place of that name: {', '.join(states[:MAX_STATES_LISTED])}{more})"
            )
    return None


def _typo_hint(text: str) -> str | None:
    """Return a "did you mean" message when a place name is close to a known city, or None.

    Without it Nominatim matches loose words and can return a place far away ("collage station" became a shop in
    California). Names with digits (street addresses) are left to Nominatim.
    """
    if any(ch.isdigit() for ch in text):
        return None
    city_state = _split_city_state(text)
    if city_state:
        name, state = city_state
    elif _BARE_NAME_RE.fullmatch(text):
        name, state = text, ""
    else:
        return None
    name = re.sub(r"\s+", " ", name.strip().lower())
    pool = sorted(
        {
            known
            for known, known_state in city_index()
            if (not state or known_state == state) and abs(len(known) - len(name)) <= TYPO_MAX_LENGTH_DIFFERENCE
        }
    )
    matches = [
        m for m in difflib.get_close_matches(name, pool, n=TYPO_MAX_SUGGESTIONS, cutoff=TYPO_CUTOFF) if m != name
    ]
    if not matches:
        return None
    if state:
        options = " or ".join(f"'{m.title()}, {state}'" for m in matches)
    else:
        suggestions = []
        for m in matches:
            states = states_for_city(m)
            more = ", ..." if len(states) > MAX_STATES_PER_SUGGESTION else ""
            suggestions.append(f"{m.title()} ({', '.join(states[:MAX_STATES_PER_SUGGESTION])}{more})")
        options = " or ".join(suggestions)
    return f"could not find '{text}' in the USA - did you mean {options}? Send it as 'City, ST'"


def _nominatim(text: str) -> tuple[tuple[float, float, str] | None, int]:
    """Return ((lat, lon, label) or None, number of API calls made)."""
    key = "geo:" + hashlib.sha1(text.lower().encode(), usedforsecurity=False).hexdigest()
    hit = cache.get(key)
    if hit:
        return hit, 0
    params: dict[str, str | int] = {"q": text, "format": "json", "limit": 1, "countrycodes": "us"}
    resp = requests.get(
        settings.NOMINATIM_URL,
        params=params,
        headers={"User-Agent": settings.HTTP_USER_AGENT},
        timeout=settings.HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None, 1
    result = (float(results[0]["lat"]), float(results[0]["lon"]), results[0].get("display_name", text))
    cache.set(key, result, settings.ROUTE_CACHE_SECONDS)
    return result, 1


def resolve_location(text: str) -> Location:
    if not isinstance(text, str) or not text.strip():
        raise LocationError("location must be a non-empty string")
    text = text.strip()
    if len(text) > MAX_INPUT_LENGTH:
        raise LocationError("location is too long")
    if not _ALLOWED_RE.fullmatch(text):
        raise LocationError("location contains unsupported characters; use e.g. 'Chicago, IL', a ZIP, or 'lat,lon'")

    coords = _COORD_RE.match(text)
    if coords:
        lat, lon = float(coords.group(1)), float(coords.group(2))
        location = Location(text, lat, lon, f"{lat:.5f}, {lon:.5f}", "coordinates")
    else:
        found = _resolve_offline(text)
        if found is None:
            hint = _typo_hint(text)
            if hint:
                raise LocationError(hint)
            if not settings.GEOCODER_FALLBACK:
                raise LocationError(f"could not find '{text}' in the USA (try 'City, ST')")
            try:
                result, calls = _nominatim(text)
            except (requests.RequestException, ValueError, KeyError) as exc:
                log.warning("Nominatim failed for %r: %s", text, exc)
                raise LocationError(
                    f"could not geocode '{text}' (geocoding service unavailable); try 'City, ST'"
                ) from exc
            if not result:
                raise LocationError(f"could not find '{text}' in the USA")
            found = Location(text, result[0], result[1], result[2], "nominatim", calls)
        location = found

    if not in_usa(location.lat, location.lon):
        town, country, _ = nearest_city(location.lat, location.lon)
        raise LocationError(f"'{text}' appears to be outside the contiguous USA (nearest town: {town}, {country})")
    return location
