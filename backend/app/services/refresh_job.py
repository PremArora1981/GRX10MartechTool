"""Engagement-wide data refresh.

Re-pulls every enabled, runnable connector in an engagement and re-sizes the
cells from the fresh data — the "refresh now" the user runs after enabling new
connectors, or whenever they want the model brought up to date. Idempotent
(content-hash dedupe on raw rows), concurrent across sources, and it reuses the
same land + re-size path as the per-connector "Pull data now".

Runs in the background (FastAPI BackgroundTasks locally; a Render Job in cloud
via the same launch path as the seed). Web-search-only sources are skipped here
(they're the seed's job); this refreshes REAL connectors.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging

from sqlalchemy import text

from backend.app.db import get_session
from backend.app.services import credential_service

logger = logging.getLogger("grx10.services.refresh_job")

REFRESH_WORKERS = 4
_PULL_SECONDS = 25.0
_MAX_ROWS = 500


def launch_refresh(engagement_id: str, *, background=None) -> dict:
    if background is not None:
        background.add_task(run_refresh, engagement_id)
        return {"launched": True, "mode": "local_background",
                "detail": "Refreshing: re-pulling enabled connectors and re-sizing cells in the background."}
    run_refresh(engagement_id)
    return {"launched": True, "mode": "local_sync", "detail": "Refresh done."}


def run_refresh(engagement_id: str) -> dict:
    """Pull every enabled runnable connector for the engagement, then re-size."""
    # Imports here to avoid a circular import at module load (connectors imports deps).
    from connectors.registry import discover, get_connector
    from backend.app.routers.connectors import (
        _resize_after_pull, _RAW_TABLE_COLUMNS,
    )
    discover()

    session = next(get_session())
    try:
        sources = [dict(r) for r in session.execute(
            text(
                "SELECT source_id, publisher, url_pattern, auth, auth_secret_ref, class, "
                "       connector, raw_table, access_method, notes "
                "FROM sources WHERE engagement_id = :e AND enabled = TRUE "
                "  AND connector IS NOT NULL AND connector <> 'web_search' "
                "  AND access_method <> 'web_search'"
            ),
            {"e": engagement_id},
        ).mappings().all()]
    finally:
        session.close()

    logger.info("refresh for %s: %d enabled connectors, %d workers",
                engagement_id, len(sources), REFRESH_WORKERS)
    totals = {"sources": len(sources), "landed": 0, "resized": 0, "ok": 0, "failed": 0}

    def _one(src: dict) -> dict:
        return _refresh_source(engagement_id, src, get_connector, _resize_after_pull,
                               set(_RAW_TABLE_COLUMNS.get(src.get("raw_table"), [])))

    with concurrent.futures.ThreadPoolExecutor(max_workers=REFRESH_WORKERS) as pool:
        for fut in concurrent.futures.as_completed([pool.submit(_one, s) for s in sources]):
            try:
                r = fut.result()
                totals["landed"] += r["landed"]
                totals["resized"] += r["resized"]
                totals["ok"] += 1 if r["ok"] else 0
                totals["failed"] += 0 if r["ok"] else 1
            except Exception as exc:  # noqa: BLE001
                totals["failed"] += 1
                logger.warning("refresh source failed: %s", exc)

    logger.info("refresh complete for %s: %s", engagement_id, totals)
    return {"engagement_id": engagement_id, **totals}


def _refresh_source(engagement_id, src, get_connector, resize_fn, cols) -> dict:
    import time
    raw_table = src.get("raw_table")
    if not raw_table:
        return {"landed": 0, "resized": 0, "ok": False}
    session = next(get_session())
    try:
        credential = None
        if src.get("auth_secret_ref"):
            credential = credential_service.retrieve(session, cred_ref=src["auth_secret_ref"])
        connector = get_connector(src, credential)
        if connector is None:
            return {"landed": 0, "resized": 0, "ok": False}
        try:
            probe = connector.probe()
            if getattr(probe, "status", "UNREACHABLE") != "OK":
                return {"landed": 0, "resized": 0, "ok": False}
        except Exception:  # noqa: BLE001
            return {"landed": 0, "resized": 0, "ok": False}

        taxonomy = [dict(r._mapping) for r in session.execute(
            text("SELECT sc.subcategory_id, sc.name, sc.hs_codes, sc.regulatory_codes, f.name AS family "
                 "FROM taxonomy_subcategories sc JOIN taxonomy_families f USING (family_id) "
                 "WHERE sc.engagement_id=:e"), {"e": engagement_id})]
        geographies = [dict(r._mapping) for r in session.execute(
            text("SELECT geography_id, country, segment FROM geographies WHERE engagement_id=:e"),
            {"e": engagement_id})]

        landed = 0
        deadline = time.monotonic() + _PULL_SECONDS
        for raw in connector.pull(taxonomy=taxonomy, geographies=geographies, since=None):
            if time.monotonic() > deadline or landed >= _MAX_ROWS:
                break
            payload = json.dumps(raw, default=str, ensure_ascii=False)
            new_row = session.execute(
                text(f"INSERT INTO {raw_table} (source_id, engagement_id, raw_json) "
                     f"SELECT :sid, :e, CAST(:p AS jsonb) WHERE NOT EXISTS "
                     f"(SELECT 1 FROM {raw_table} WHERE source_id=:sid AND engagement_id=:e "
                     f" AND md5(raw_json::text)=md5(CAST(:p AS jsonb)::text)) RETURNING raw_id"),
                {"sid": src["source_id"], "e": engagement_id, "p": payload}).first()
            if new_row is None:
                continue
            landed += 1
            try:
                norm = connector.normalize(raw)
            except Exception:  # noqa: BLE001
                norm = None
            if norm:
                setc = {k: v for k, v in norm.items()
                        if k in cols and k not in ("raw_id", "source_id", "engagement_id", "raw_json")}
                if setc:
                    session.execute(
                        text(f"UPDATE {raw_table} SET " + ", ".join(f"{k}=:{k}" for k in setc)
                             + " WHERE raw_id=:rid"), {**setc, "rid": new_row.raw_id})
        resized = 0
        if landed:
            resized = resize_fn(session, engagement_id, src["source_id"], raw_table,
                                src.get("class") or "B")
        session.commit()
        return {"landed": landed, "resized": resized, "ok": True}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.warning("refresh source %s failed: %s", src.get("source_id"), exc)
        return {"landed": 0, "resized": 0, "ok": False}
    finally:
        session.close()
