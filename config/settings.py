"""Django settings for the fuel route planner."""

import os
from pathlib import Path

from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent

# No hard-coded key, debug off and local hosts only unless the environment says otherwise.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_random_secret_key()
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(",")

INSTALLED_APPS = ["routeplanner"]
# No CSRF middleware: the API is stateless (no sessions or cookies), and CSRF would reject API-client POSTs.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
STATIC_URL = "static/"

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
# Only used when a place can't be resolved offline.
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
GEOCODER_FALLBACK = os.environ.get("GEOCODER_FALLBACK", "1") == "1"
HTTP_TIMEOUT = (3.05, 8)  # (connect, read) seconds
# polyline6 is several times smaller than GeoJSON on the wire; both formats are decoded.
OSRM_GEOMETRIES = os.environ.get("OSRM_GEOMETRIES", "polyline6")
HTTP_USER_AGENT = "fuel-route-planner/1.0 (coding assessment)"

VEHICLE_MAX_RANGE_MILES = 500.0
VEHICLE_MPG = 10.0
# Fuel at the start; the plan refuels to this level before arriving, so every mile of the trip is billed.
VEHICLE_STARTING_FUEL_GALLONS = float(os.environ.get("VEHICLE_STARTING_FUEL_GALLONS", "5"))
# The default starting fuel is capped at this share of the tank; a value sent by the caller is used as is.
STARTING_FUEL_MAX_TANK_SHARE = 0.25
# Station search distances from the route, widened step by step if a plan is impossible.
CORRIDOR_STEPS_MILES = (10.0, 25.0, 50.0)
# With detour fuel billed a wider search is safe: a far station is used only when it is cheaper overall.
DETOUR_CORRIDOR_STEPS_MILES = (25.0, 50.0)
ROUTE_CACHE_SECONDS = 60 * 60 * 24
# Avoids chains of tiny top-ups when detours are not billed; 0 gives the exact optimum.
MIN_PRICE_SAVINGS_PER_GALLON = float(os.environ.get("MIN_PRICE_SAVINGS_PER_GALLON", "0.05"))
ROUTE_ALTERNATIVES = int(os.environ.get("ROUTE_ALTERNATIVES", "3"))
RECOMMEND_MIN_SAVINGS_USD = float(os.environ.get("RECOMMEND_MIN_SAVINGS_USD", "5"))
# "major": cities with at least 15,000 people; "all": every town.
CITY_SCOPE = os.environ.get("CITY_SCOPE", "major")
CITY_RADIUS_MILES = {"major": 5.0, "all": 5.0}
