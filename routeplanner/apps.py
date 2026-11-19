import os
import sys
import threading

from django.apps import AppConfig

WARM_UP_DELAY_SECONDS = 1.0


def warm_caches() -> None:
    """Load the station index, city lists and place data so the first request is not slow."""
    from .services import cities, geocode, geography, places, stations

    stations.get_index()
    cities.get_cities("major")
    cities.get_cities("all")
    geocode.city_index()
    geography.nearest_city(40.0, -100.0)
    places.suggest("warm up")


class RoutePlannerConfig(AppConfig):
    name = "routeplanner"

    def ready(self) -> None:
        # Under runserver, warm in the background once the autoreloader child process is running.
        if "runserver" in sys.argv and os.environ.get("RUN_MAIN") == "true":
            timer = threading.Timer(WARM_UP_DELAY_SECONDS, self._safe_warm)
            timer.daemon = True
            timer.start()

    @staticmethod
    def _safe_warm() -> None:
        try:
            warm_caches()
        except Exception:  # e.g. the database is not migrated yet; never block startup
            pass
