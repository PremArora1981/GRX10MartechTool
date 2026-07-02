"""Feature routers package.

Each module here exposes a module-level ``router: APIRouter`` and is
auto-discovered + included by ``backend.app.main.register_routers`` at startup.
Drop a new ``<feature>.py`` in this directory and it is wired with no edit to
``main.py``. Sibling agents own the individual modules (cells, sources/connectors,
players, assumptions, reports, status, settings, credentials, auth).
"""
