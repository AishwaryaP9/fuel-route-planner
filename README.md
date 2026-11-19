# Fuel Route Planner

A Django API that plans a road trip between two places in the USA. For each proposed route it returns the cities
passed, the cheapest places to buy fuel (500-mile range, 10 mpg by default), the total fuel cost and a map. A web page
at `/` offers the same plan behind a search form.

## Assignment requirements
| Requirement | How it is met |
|---|---|
| Latest stable Django | Django 6.1.3, pinned in `requirements.txt`. |
| Start and finish inside the USA | `"City, ST"`, `"City, State"`, ZIP code, `"lat,lon"` or a street address. Places outside the contiguous USA get a 400 error. |
| Map of the route | Every route has `map_geojson` (GeoJSON) and a `map_html_url` link to a map page. |
| Optimal fuel stops by price | The optimizer picks where and how much to buy so the fuel cost is lowest (see How it works). |
| 500-mile range, several fuel-ups | `max_range_miles` defaults to 500; a long trip gets as many stops as it needs, listed in `fuel.stops`. |
| Total fuel cost at 10 mpg | `mpg` defaults to 10; each route has `fuel.total_cost_usd`. |
| Supplied fuel price file | `data/fuel-prices.csv`, loaded by `python manage.py import_fuel_prices` (see Data). |
| Free routing API | The [OSRM](https://project-osrm.org) demo server; no API key. |
| Quick responses | A repeated request is answered from the cache with no outside calls. A new request makes one routing call; stations are searched in an in-memory index loaded once per process. |
| Few calls to the routing API | One call per new start and finish pair, zero when cached. Each response shows the count in `meta.routing_api_calls`. |

## Requirements
- Python 3.12 or newer
- Internet access for routing (OSRM demo server); the web page also loads map tiles (OpenStreetMap) and Leaflet (unpkg)

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py import_fuel_prices   # loads data/fuel-prices.csv into SQLite
```
**Note:** `db.sqlite3` is not in the repository, so `python manage.py import_fuel_prices` must be run once after
`migrate`.

## Data
`data/fuel-prices.csv` has 8,151 rows. The import keeps one row per truck stop (the lowest price when a stop is listed
more than once) and drops rows outside the USA, which leaves 6,626 stations. The file has no coordinates, so each
station is placed at its city's location using offline ZIP and city data. The import prints
`8151 rows -> 6626 unique US stations -> 6626 geocoded, 0 skipped`.

## Running
```bash
python manage.py runserver            # development
gunicorn config.wsgi                  # production
```
Open `http://127.0.0.1:8000/` for the web page, or call the API, for example
`http://127.0.0.1:8000/api/route/?start=Dallas, TX&finish=Chicago, IL&pretty=1`.
`postman_collection.json` contains ready-made requests.

### Try it
```bash
curl -G "http://127.0.0.1:8000/api/route/" --data-urlencode "start=Dallas, TX" --data-urlencode "finish=Chicago, IL" \
     --data-urlencode "pretty=1"

curl -X POST "http://127.0.0.1:8000/api/route/" -H "Content-Type: application/json" \
     -d '{"start": "Dallas, TX", "finish": "Chicago, IL"}'
```
In the response, look at `routes` (one entry per proposed route), `fuel.stops` and `fuel.total_cost_usd` in each
route, `recommended_route_id`, and `meta.routing_api_calls` (1 the first time, 0 when the same request is sent again).

## API
| Method | URL | Returns |
|---|---|---|
| GET, POST, HEAD | `/api/route/` | The plan as JSON. POST accepts a JSON object or a form with the same fields. |
| GET, POST, HEAD | `/api/route/map/` | The plan as a map page; `route=N` selects a route. |
| GET, POST, HEAD | `/` | The web page: a search form over a map of the USA, then the plan. |

`start` and `finish` accept `"City, ST"`, `"City, State"`, a ZIP code, `"lat,lon"` or a street address.

| Field | Default | Meaning |
|---|---|---|
| `mpg` | 10 | Fuel economy (0.5–200). |
| `max_range_miles` | 500 | Distance on a full tank (50–2000). Tank size is `max_range_miles / mpg`. |
| `starting_fuel_gallons` | 5, at most a quarter of the tank | Fuel at the start (0 to the tank size). |
| `alternatives` | 3 | Routes to request (1–3); the routing service may return fewer. |
| `city_scope` | `major` | `major`: cities with at least 15,000 people; `all`: every town. |
| `city_radius_miles` | 5 | How close to the route a city must be to be listed (0.1–50). |
| `include_cities` | true | `false` leaves out the city lists. |
| `include_detour_fuel` | true | Bill the fuel to drive from the route to each station and back. |
| `allow_estimated_stops` | true | Use estimated stops where the price data has no station. |
| `pretty` | false | Indent the JSON. |

Response (one route and one stop shown, values from Dallas, TX to Chicago, IL):
```jsonc
{
  "start": {"query": "Dallas, TX", "label": "Dallas, TX", "lat": 32.78941, "lon": -96.7896},
  "finish": {"query": "Chicago, IL", "label": "Chicago, IL", "lat": 41.85068, "lon": -87.67915},
  "recommended_route_id": 2,
  "recommendation_reason": "cheapest fuel: saves $13.16 vs the fastest route (+0.05 h)",
  "route_count": 2,
  "city_scope": "major",
  "warnings": [],
  "routes": [{
    "route_id": 2, "is_fastest": false, "is_cheapest_fuel": true, "is_recommended": true,
    "distance_miles": 920.6, "duration_hours": 17.17,
    "fuel_error": null,
    "fuel": {
      "vehicle": {"max_range_miles": 500.0, "mpg": 10.0, "tank_capacity_gallons": 50.0, "starting_fuel_gallons": 5.0,
                  "requested_starting_fuel_gallons": 5.0, "starting_fuel_adjusted": false},
      "assumption": "The vehicle starts with 5 gal (50 mi) of fuel ...",
      "include_detour_fuel": true, "detour_miles_total": 4.8, "detour_cost_usd": 1.39,
      "estimated_stops": 0, "estimated_cost_usd": 0.0,
      "fuel_needed_gallons": 92.54, "gallons_purchased": 92.54, "total_cost_usd": 266.83,
      "station_search_radius_miles": 25.0,
      "stops": [{"order": 1, "station_id": 72773, "name": "RaceTrac #2626", "address": "I-20 Exit 472",
                 "city": "Dallas", "state": "TX", "lat": 32.78941, "lon": -96.7896, "distance_from_route_miles": 0.0,
                 "estimated": false, "price_per_gallon": 2.864, "mile_marker": 0.0, "detour_miles": 0.0,
                 "detour_gallons": 0.0, "gallons_purchased": 45.0, "cost_usd": 128.88}]
    },
    "cities_count": 73,
    "cities": [{"city": "Dallas", "state": "TX", "lat": 32.7831, "lon": -96.8067, "mile_marker": 0.3,
                "distance_from_route_miles": 1.0, "population": 1326087}],
    "map_geojson": {"type": "FeatureCollection", "features": ["..."]},
    "map_html_url": "http://127.0.0.1:8000/api/route/map/?start=Dallas%2C+TX&finish=Chicago%2C+IL&route=2"
  }],
  "map_html_url": "...",
  "meta": {"routing_api_calls": 1, "geocoding_api_calls": 0, "route_cache_hit": false, "plan_cache_hit": false,
           "elapsed_ms": 2941.7}  // time spent on this request; it varies
}
```

## API calls per search
| Search | Routing calls | Geocoding calls |
|---|---|---|
| New pair given as `"City, ST"`, ZIP code or `"lat,lon"` | 1 | 0 |
| The same pair again, the same pair with other options, or the map page after the API | 0 | 0 |
| New pair with a street address | 1 | 1 per address (cached) |
| Typing in the search box | 0 | 0 |

The most a search can make is 3 calls: one geocoding call for each of two street addresses and one routing call. The
search box filters its suggestions in the browser, so typing makes no requests. Map tiles (OpenStreetMap) and Leaflet
(unpkg) are loaded by the browser from those servers; they are not calls made by the API.

## Web page
`/` shows a map of the USA with a search form: Start, Finish and Search, plus Options for every field above.
Place names are suggested from a list of about 2,000 US cities embedded in the page, so typing makes no requests.
The search reloads the page with the plan: route tabs, total cost, stops and cities next to the map. The URL holds the
search, so a result can be shared. Errors appear inside the form, with the same status codes as the API.
`/api/route/map/` is the same page; every API response links to it in `map_html_url`.

## Errors
Errors from `/api/route/` are JSON `{"error": "..."}`:

| Status | When |
|---|---|
| 400 | Invalid input: missing place, bad number, invalid JSON, a place outside the USA, an unknown or ambiguous place (with a "did you mean" hint for close names), or a street address while the geocoding service is unavailable |
| 422 | No drivable route between the two places |
| 502 | The routing service returned an error or an invalid answer |
| 503 | The routing service is unreachable, slow or rate-limited (with `Retry-After: 5`) |

The map page and `/` show these errors inside the form as an HTML page with the same status code.

These responses are Django's own and are not JSON:

| Status | When | Body |
|---|---|---|
| 400 | Request body larger than 2.5 MB | HTML "Bad Request" page |
| 404 | Unknown URL | HTML "Not Found" page |
| 405 | Method not allowed (for example PUT or DELETE) | Empty |
| 500 | Unexpected server error | HTML "Server Error" page |

A route the vehicle cannot complete is still returned, with `fuel: null` and a `fuel_error` explaining why.

## How it works
- **One routing call.** A single OSRM request returns the main route and its alternatives with full geometry, so a whole
  plan costs one call. Plans and routes are cached for 24 hours, and a repeated request makes no call.
- **Offline geocoding.** Places and fuel stations are resolved with offline ZIP and city data (`zipcodes`,
  `geonamescache`). Only free-text addresses use the Nominatim geocoder.
- **Stations along the route.** Stations (6,626 after import) are kept in memory; a KD-tree finds those within
  25 miles of the route and their mile markers, widening to 50 miles if needed (10 miles first when detours are not
  billed).
- **Optimizer.** Each stop costs a detour from the route and back, so the cheapest plan is found by dynamic programming
  over the fuel level on a 0.2-mile grid. Without detour billing a greedy rule is used: buy just enough to reach a
  cheaper station, otherwise fill up (optimal when `MIN_PRICE_SAVINGS_PER_GALLON` is 0; the default $0.05 skips tiny
  top-ups).
- **Billing.** The vehicle refuels to its starting level before arriving, so every mile driven is billed.
- **Estimated stops.** Where the price data has no station within reach (for example on the West Coast), virtual stops
  priced at the state's average are added and marked `"estimated": true`.
- **Recommendation.** The cheapest route is recommended only if it saves at least $5 over the fastest route; routes
  priced only from real stations are preferred.

## Assumptions and limits
- Stations are placed at the centre of their city (the price file has no coordinates), and detours are straight lines.
- Routing uses the OSRM car profile; truck restrictions and driving-time rules are not modelled.
- Fuel economy is constant, there is no safety reserve, and only fuel is costed.
- A truck stop listed several times keeps its lowest price.
- Only the contiguous USA is supported.
- The cache is per process; with several workers each one fetches a route once. A shared cache such as Redis avoids this.

## Configuration
Environment variables: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `OSRM_BASE_URL`, `OSRM_GEOMETRIES`,
`NOMINATIM_URL`, `GEOCODER_FALLBACK`, `ROUTE_ALTERNATIVES`, `CITY_SCOPE`, `MIN_PRICE_SAVINGS_PER_GALLON`,
`VEHICLE_STARTING_FUEL_GALLONS` and `RECOMMEND_MIN_SAVINGS_USD`.

## Tests
```bash
python manage.py test   # 71 tests
```
The tests replace the routing service with fake responses, so they run without network access.