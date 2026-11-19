"""WSGI entry point; also loads the in-memory indexes once per worker."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()

try:
    from routeplanner.apps import warm_caches

    warm_caches()
except Exception:  # e.g. the database is not migrated yet: the first request loads them instead
    pass
