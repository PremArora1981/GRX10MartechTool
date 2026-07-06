"""FastAPI application entrypoint.

This is the backend skeleton every other backend agent plugs into. It wires:

* CORS (origins from settings — the Next.js frontend URL + dev localhost);
* a ``/health`` endpoint (Render health check + status-page freshness probe);
* **automatic router registration**: any module in ``backend/app/routers/`` that
  exposes a module-level ``router`` (an ``APIRouter``) is discovered and included
  at startup. Agents adding a feature drop a file in ``routers/`` and it is wired
  with no edit here. An explicit allow/skip env hook is provided for control.

Routers expected to land in ``backend/app/routers/`` (built by sibling agents):
  ``cells.py`` · ``sources.py`` (connectors admin) · ``players.py`` ·
  ``assumptions.py`` · ``reports.py`` · ``status.py`` · ``settings.py`` ·
  ``credentials.py`` · ``auth.py``.

Start command (Procfile / render.yaml):
  ``uvicorn backend.app.main:app --host 0.0.0.0 --port $PORT``
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from datetime import datetime, timezone

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.config import settings
from backend.app.db import ping
from backend.app.schemas import HealthResponse

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("grx10.api")

API_VERSION = "1.0.0"

app = FastAPI(
    title="GRX10 Automated Market Research Tool — API",
    version=API_VERSION,
    description=(
        "Config-driven market-sizing platform. Every number is drillable "
        "cell -> estimate -> source -> raw; confidence is computed only by the "
        "cell_triangulation_summary view."
    ),
)

# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Client-usage email alert (alpha). Inert unless ZEPTOMAIL_TOKEN/ALERT_EMAIL_FROM
# are configured; never blocks or slows a request.
from backend.app.usage_alert import UsageAlertMiddleware  # noqa: E402

app.add_middleware(UsageAlertMiddleware)


@app.on_event("startup")
async def _start_usage_digest_scheduler() -> None:
    """Launch the always-on daily usage-digest scheduler (in-process; the web
    service is a paid, always-on plan). Inert if email is unconfigured."""
    import asyncio

    from backend.app.daily_usage_digest import scheduler_loop

    asyncio.create_task(scheduler_loop())


# --------------------------------------------------------------------------- #
# Health endpoint
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """Liveness + dependency check for Render and the in-app status page."""
    db_up = ping()
    return HealthResponse(
        status="ok" if db_up else "degraded",
        version=API_VERSION,
        database="up" if db_up else "down",
        auth_configured=settings.auth_configured,
        time=datetime.now(timezone.utc),
    )


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    """Tiny landing payload (kept so a bare GET / doesn't 404)."""
    return {"service": "grx10-mr-api", "version": API_VERSION, "docs": "/docs"}


# --------------------------------------------------------------------------- #
# Router auto-registration
# --------------------------------------------------------------------------- #
# === routers registered by integrator ===
# Feature routers live in backend/app/routers/<name>.py and expose `router`.
# They are auto-included below. To wire one explicitly instead, import it here
# and call app.include_router(<module>.router) — but the auto-include already
# handles every module that follows the convention, so manual edits are rarely
# needed. Set GRX10_DISABLE_ROUTERS="cells,reports" to skip specific modules.
# =========================================================================
def register_routers(application: FastAPI) -> list[str]:
    """Discover and include every ``router`` in ``backend.app.routers``.

    A router module must expose a module-level ``router: APIRouter``. Modules that
    fail to import (e.g. still being written by another agent) are logged and
    skipped so one broken feature never takes down the whole API. Returns the list
    of successfully-registered module names.
    """
    import os

    disabled = {
        name.strip()
        for name in os.environ.get("GRX10_DISABLE_ROUTERS", "").split(",")
        if name.strip()
    }
    registered: list[str] = []

    try:
        routers_pkg = importlib.import_module("backend.app.routers")
    except ModuleNotFoundError:
        logger.info("no backend.app.routers package yet — only meta endpoints active")
        return registered

    for mod_info in pkgutil.iter_modules(routers_pkg.__path__):
        name = mod_info.name
        if name.startswith("_") or name in disabled:
            continue
        full = f"backend.app.routers.{name}"
        try:
            module = importlib.import_module(full)
        except Exception as exc:  # noqa: BLE001 — isolate a broken sibling router
            logger.warning("skipping router %s (import failed): %s", full, exc)
            continue
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            application.include_router(router)
            registered.append(name)
            logger.info("registered router: %s", full)
        else:
            logger.debug("module %s has no `router` APIRouter — skipped", full)

    return registered


REGISTERED_ROUTERS: list[str] = register_routers(app)


@app.get("/meta/routers", tags=["meta"])
def list_registered_routers() -> dict[str, list[str]]:
    """Introspection helper: which feature routers are live in this process."""
    return {"registered": REGISTERED_ROUTERS}
