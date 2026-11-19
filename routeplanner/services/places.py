"""Place suggestions for the search box, built from the same offline data the geocoder uses.

Every suggested label therefore resolves offline when it is submitted.
"""

import bisect
import re
from typing import TypedDict

import zipcodes

from .cities import get_cities
from .geocode import state_code

NON_CONUS_STATES = {"AK", "HI", "PR", "VI", "GU", "AS", "MP", "AA", "AE", "AP", "FM", "MH", "PW"}
MIN_CHARS = 2
LIMIT = 8
MIN_CHARS_FOR_WORD_MATCH = 3
PAGE_LIST_SIZE = 2000  # cities embedded in the search page, most populous first
_ABBREVIATIONS = ((r"^st\.? ", "saint "), (r"^ste\.? ", "sainte "), (r"^ft\.? ", "fort "), (r"^mt\.? ", "mount "))


class Suggestion(TypedDict):
    label: str
    value: str


CityRow = tuple[str, int, str, str]  # normalised name, minus population, label, state


def _normalise(name: str) -> str:
    """Return 'saint louis' for 'St. Louis' or 'st louis' (the ZIP database spells the prefixes out)."""
    name = re.sub(r"\s+", " ", name.strip().lower())
    for pattern, replacement in _ABBREVIATIONS:
        name = re.sub(pattern, replacement, name)
    return name


class _Index:
    def __init__(self) -> None:
        spelling: dict[tuple[str, str], str] = {}
        zip_rows = []
        for record in zipcodes.list_all():
            if not record["lat"] or not record["long"] or record["state"] in NON_CONUS_STATES:
                continue
            spelling.setdefault((record["city"].lower(), record["state"]), record["city"])
            zip_rows.append((record["zip_code"], f"{record['city']}, {record['state']}"))
        major = get_cities("major")
        population: dict[tuple[str, str], int] = {}
        nicer_spelling: dict[tuple[str, str], str] = {}  # e.g. "McAllen" where the ZIP database has "Mcallen"
        for name, state, people in zip(major.names, major.states, major.population or [], strict=True):
            key = (_normalise(name), state)
            if people > population.get(key, 0):
                population[key], nicer_spelling[key] = people, name
        rows: list[CityRow] = []
        for (name, state), shown in spelling.items():
            key = (_normalise(name), state)
            nicer = nicer_spelling.get(key)
            label = nicer if nicer and nicer.lower() == shown.lower() else shown
            rows.append((_normalise(name), -population.get(key, 0), f"{label}, {state}", state))
        self.cities = sorted(rows)
        # [label, match key] pairs for the page; the key is normalised like a query ("saint louis, mo").
        self.page_list: list[list[str]] = []
        seen: set[str] = set()
        for row in sorted((row for row in rows if row[1] < 0), key=lambda row: (row[1], row[2])):
            if row[2] not in seen:
                seen.add(row[2])
                self.page_list.append([row[2], f"{row[0]}, {row[3].lower()}"])
            if len(self.page_list) == PAGE_LIST_SIZE:
                break
        self.names = [row[0] for row in self.cities]
        self.zips = sorted(set(zip_rows))
        self.zip_codes = [code for code, _ in self.zips]


_index: _Index | None = None


def _get() -> _Index:
    global _index
    if _index is None:
        _index = _Index()
    return _index


def page_list() -> list[list[str]]:
    """Return the [label, match key] pairs embedded in the search page (built once, then cached)."""
    return _get().page_list


def suggest(text: str, limit: int = LIMIT) -> list[Suggestion]:
    """Return up to `limit` places matching the text typed so far.

    Names that start with the text come first (biggest cities first), then names with a later word starting with it.
    "austin, t" keeps states starting with "t"; digits suggest ZIP codes.
    """
    query = re.sub(r"\s+", " ", (text or "").strip().lower())
    if len(query) < MIN_CHARS:
        return []
    index = _get()
    if query.isdigit():
        start = bisect.bisect_left(index.zip_codes, query)
        zip_suggestions: list[Suggestion] = []
        for code, place in index.zips[start : start + limit]:
            if not code.startswith(query):
                break
            zip_suggestions.append({"label": f"{code} - {place}", "value": code})
        return zip_suggestions

    name, state_part = query, ""
    if "," in query:
        name, state_part = (part.strip() for part in query.rsplit(",", 1))
        if not name:
            return []
    name = _normalise(name)

    def state_matches(state: str) -> bool:
        if not state_part:
            return True
        full = state_code(state_part)
        return state == full if full else state.lower().startswith(state_part)

    position = bisect.bisect_left(index.names, name)
    prefix_matches = []
    while position < len(index.cities) and index.cities[position][0].startswith(name):
        if state_matches(index.cities[position][3]):
            prefix_matches.append(index.cities[position])
        position += 1
    prefix_matches.sort(key=lambda row: (row[1], row[0], row[3]))
    picked = prefix_matches[:limit]
    if len(picked) < limit and len(name) >= MIN_CHARS_FOR_WORD_MATCH:
        seen = {row[2] for row in picked}
        word = " " + name  # "east saint louis" matches, "bethel park" does not match "el pa"
        word_matches = [row for row in index.cities if word in row[0] and row[2] not in seen and state_matches(row[3])]
        word_matches.sort(key=lambda row: (row[1], row[0], row[3]))
        picked += word_matches[: limit - len(picked)]
    return [{"label": row[2], "value": row[2]} for row in picked]
