"""Pipeline orchestrator — the Render Cron Job entrypoint.

Runs the ordered, idempotent stages defined by the v1 spec:

    build_cells -> ingest -> normalize -> size_cells -> score_confidence -> probe_health

Each stage is a self-contained function that opens its own DB scope, so stages
can also be run individually (``--stages size_cells,score_confidence``) for
debugging or partial re-runs. Everything is idempotent:

* **build_cells** materialises the cell grid (subcategory × geography × year)
  from the spine and an anchor year + forecast horizon, with
  ``ON CONFLICT DO NOTHING`` so existing cells (and their sized TAM/confidence)
  are never clobbered.
* **ingest** pulls verbatim payloads, lands them into the right ``raw_*`` table
  (deduped by ``(source_id, md5(raw_json))``) **and** fills the normalised typed
  columns for each newly-landed row in the same step — re-running never
  duplicates a verbatim payload.
* **normalize** is a backfill: it fills typed columns for any raw rows that are
  still un-normalised (all typed columns NULL); re-applying yields identical
  values.
* **size_cells** runs every *applicable* method (one whose required raw tables
  hold data) against every active cell and upserts on ``cell_triangulation``'s
  composite key ``(cell_id, method_code, source_id)`` — the spec's idempotent
  fact-row key. Each method runs inside its own SAVEPOINT so one bad method
  cannot poison a cell's other estimates.
* **score_confidence** refreshes the ``cell_triangulation_summary`` materialised
  view (the *only* place confidence is computed — never a write-time human
  override), then projects its verdict onto ``cells``, **capped per method
  ``confidence_cap``** (so ``web_search_extraction`` can never lift a cell above
  LOW).
* **probe_health** re-probes every source into the 7-state taxonomy, persists
  ``last_probe_*`` on ``sources``, and emits a Slack webhook for unhealthy
  connectors plus a 🟠 budget pre-warning at ~80% of the configured ceiling.

Connectors and methods are imported **lazily** from ``connectors.registry`` /
``methods.registry`` so the orchestrator stays runnable even while those plug-in
layers are still being built in parallel. Missing pieces degrade gracefully
(logged + skipped) — never fabricated.

Run::

    python -m pipeline.run                          # all stages, in order
    python -m pipeline.run --stages ingest          # a subset
    python -m pipeline.run --since 2025-01-01        # ingestion lookback hint
    python -m pipeline.run --anchor-year 2024 --horizon-years 2
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger("grx10.pipeline")

# Raw tables are interpolated into SQL by name (from DB config), so they are
# validated against this allow-list to keep identifier handling injection-safe.
ALLOWED_RAW_TABLES: frozenset[str] = frozenset({
    "raw_trade_flows", "raw_regulatory", "raw_filings", "raw_transcripts",
    "raw_shipments", "raw_external_metrics", "raw_industry_reports", "raw_patents",
    "raw_procurement", "raw_standards", "raw_news", "raw_signals",
})
_FIXED_RAW_COLS: frozenset[str] = frozenset({"raw_id", "source_id", "accessed_at", "raw_json"})

STAGE_ORDER = [
    "build_cells", "ingest", "normalize", "size_cells", "score_confidence", "probe_health",
]

# Cell-grid defaults (overridable via env or CLI). The anchor is the base year a
# cell is sized for; the horizon is the number of forecast years beyond it.
_DEFAULT_HORIZON_YEARS = 0


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def get_engine() -> Engine:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("DATABASE_URL is not set — cannot run the pipeline.")
    return create_engine(_normalize_db_url(raw), future=True, pool_pre_ping=True)


# --------------------------------------------------------------------------- #
# Lazy plug-in resolution (resilient to parallel builds)
# --------------------------------------------------------------------------- #
def _resolve_connector_class(connector_name: str | None) -> type | None:
    """Look up a connector class by module name from ``connectors.registry``.

    Tries the common registry shapes (``get``/``get_connector`` callables or a
    ``REGISTRY``/``CONNECTORS`` mapping) and returns ``None`` if the registry or
    the connector is not yet available.
    """
    if not connector_name:
        return None
    try:
        registry = importlib.import_module("connectors.registry")
    except Exception as exc:  # noqa: BLE001
        logger.warning("connectors.registry not importable yet: %s", exc)
        return None
    for attr in ("get_connector", "get", "resolve"):
        fn = getattr(registry, attr, None)
        if callable(fn):
            try:
                cls = fn(connector_name)
                # ``get_connector`` may return an *instance* for a name-only call
                # on some registries; only accept a class here.
                if isinstance(cls, type):
                    return cls
            except Exception:  # noqa: BLE001
                pass
    for attr in ("REGISTRY", "CONNECTORS", "registry"):
        mapping = getattr(registry, attr, None)
        if isinstance(mapping, dict) and connector_name in mapping:
            return mapping[connector_name]
    logger.warning("connector %r not found in registry", connector_name)
    return None


def _resolve_method(method_code: str) -> Any | None:
    """Return an instantiated Method for ``method_code`` (or ``None`` if absent)."""
    try:
        registry = importlib.import_module("methods.registry")
    except Exception as exc:  # noqa: BLE001
        logger.warning("methods.registry not importable yet: %s", exc)
        return None
    for attr in ("get_method", "get", "resolve"):
        fn = getattr(registry, attr, None)
        if callable(fn):
            try:
                obj = fn(method_code)
                if obj is not None:
                    return obj() if isinstance(obj, type) else obj
            except Exception:  # noqa: BLE001
                pass
    for attr in ("REGISTRY", "METHODS", "registry"):
        mapping = getattr(registry, attr, None)
        if isinstance(mapping, dict) and method_code in mapping:
            obj = mapping[method_code]
            return obj() if isinstance(obj, type) else obj
    logger.warning("method %r not found in registry", method_code)
    return None


def _load_credential(source: dict[str, Any]) -> str | None:
    """Resolve a decrypted credential for a source, degrading to ``None``.

    The credential service (envelope decryption via ``CRED_MASTER_KEY``) is owned
    by another module; we call it if present. When no credential is available the
    connector's own ``probe()``/``pull()`` are responsible for returning
    ``AUTH_FAILED`` / yielding nothing — we never fabricate a secret.
    """
    cred_ref = source.get("auth_secret_ref")
    if not cred_ref:
        return None
    # The credential store (envelope decryption via CRED_MASTER_KEY) lives in
    # backend.app.services.credential_service and needs a DB session + cred_ref.
    try:
        from backend.app.db import get_session
        from backend.app.services import credential_service
    except Exception:  # noqa: BLE001 — service not importable in this context
        return None
    gen = get_session()
    session = next(gen)
    try:
        return credential_service.retrieve(session, cred_ref=cred_ref)
    except Exception:  # noqa: BLE001 — no credential / decrypt failure -> degrade
        return None
    finally:
        gen.close()


# --------------------------------------------------------------------------- #
# Shared fetch helpers
# --------------------------------------------------------------------------- #
def _fetch_sources(conn: Connection, *, enabled_only: bool) -> list[dict[str, Any]]:
    sql = ('SELECT source_id, publisher, url_pattern, auth, auth_secret_ref, '
           '"class" AS source_class, connector, refresh_cadence, raw_table, '
           'access_method, enabled, monthly_budget, quota_ceiling '
           'FROM sources')
    if enabled_only:
        sql += " WHERE enabled = true"
    sql += " ORDER BY source_id"
    return [dict(r._mapping) for r in conn.execute(text(sql))]


def _fetch_taxonomy(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        "SELECT subcategory_id, family_id, name, hs_codes, regulatory_codes "
        "FROM taxonomy_subcategories WHERE superseded_by IS NULL ORDER BY subcategory_id"))
    return [dict(r._mapping) for r in rows]


def _fetch_geographies(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        "SELECT geography_id, country, segment FROM geographies ORDER BY geography_id"))
    return [dict(r._mapping) for r in rows]


def _typed_columns(conn: Connection, raw_table: str) -> list[str]:
    """Return the normalised typed columns of a raw table (excluding fixed ones)."""
    rows = conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :t ORDER BY ordinal_position"), {"t": raw_table})
    return [r.column_name for r in rows if r.column_name not in _FIXED_RAW_COLS]


def _non_empty_raw_tables(conn: Connection) -> set[str]:
    """Return the subset of raw tables that currently hold at least one row.

    Used by ``size_cells`` to skip methods whose inputs are entirely empty,
    keeping the stage cheap on sparse engagements.
    """
    present: set[str] = set()
    for table in ALLOWED_RAW_TABLES:
        try:
            if conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1")).first():  # noqa: S608
                present.add(table)
        except Exception as exc:  # noqa: BLE001 — table may not exist yet
            logger.debug("raw-table presence check failed for %s: %s", table, exc)
    return present


def _record_probe(conn: Connection, source_id: str, status: str, detail: str | None) -> None:
    conn.execute(text(
        "UPDATE sources SET last_probe_status = :st, last_probe_at = now(), "
        "last_probe_detail = :d WHERE source_id = :id"),
        {"st": status, "d": (detail or "")[:1000], "id": source_id})


def _apply_normalized(
    conn: Connection,
    *,
    raw_table: str,
    raw_id: int,
    typed_cols: list[str],
    norm: dict[str, Any] | None,
) -> bool:
    """Write the normalised typed columns of one raw row. Returns True if it wrote."""
    set_cols = {c: v for c, v in (norm or {}).items() if c in typed_cols}
    if not set_cols:
        return False
    assignments = ", ".join(f'"{c}" = :{c}' for c in set_cols)
    params: dict[str, Any] = dict(set_cols)
    params["rid"] = raw_id
    conn.execute(text(
        f"UPDATE {raw_table} SET {assignments} WHERE raw_id = :rid"), params)
    return True


# --------------------------------------------------------------------------- #
# Stage 0 — build_cells (materialise the subcategory × geography × year grid)
# --------------------------------------------------------------------------- #
def _resolve_grid_years(anchor_year: int | None, horizon_years: int | None) -> tuple[int, int, list[int]]:
    """Resolve (anchor, horizon, [years]) from explicit args, env, then defaults.

    The anchor defaults to the latest *complete* calendar year (this year − 1,
    since most trade/filing sources lag), and the horizon to
    ``PIPELINE_HORIZON_YEARS`` (or 0 = the anchor year alone).
    """
    if anchor_year is None:
        env_anchor = os.environ.get("PIPELINE_ANCHOR_YEAR")
        anchor_year = int(env_anchor) if env_anchor else datetime.now(timezone.utc).year - 1
    if horizon_years is None:
        env_h = os.environ.get("PIPELINE_HORIZON_YEARS")
        horizon_years = int(env_h) if env_h else _DEFAULT_HORIZON_YEARS
    horizon_years = max(int(horizon_years), 0)
    years = list(range(anchor_year, anchor_year + horizon_years + 1))
    return anchor_year, horizon_years, years


def stage_build_cells(
    engine: Engine,
    *,
    anchor_year: int | None = None,
    horizon_years: int | None = None,
) -> dict[str, Any]:
    """Create the cell grid from the spine for ``anchor .. anchor+horizon`` years.

    Idempotent: ``ON CONFLICT (subcategory_id, geography_id, year) DO NOTHING``
    only inserts missing cells, so re-running never clobbers an already-sized
    cell's TAM, band, or confidence.
    """
    anchor, horizon, years = _resolve_grid_years(anchor_year, horizon_years)
    with engine.connect() as conn:
        subs = _fetch_taxonomy(conn)
        geos = _fetch_geographies(conn)

    grid_size = len(subs) * len(geos) * len(years)
    if grid_size == 0:
        logger.warning("build_cells: empty spine (subs=%d geos=%d years=%d) — nothing to build",
                       len(subs), len(geos), len(years))
        return {"cells_created": 0, "grid_size": 0, "anchor_year": anchor,
                "horizon_years": horizon, "years": years}

    insert_sql = text(
        "INSERT INTO cells (subcategory_id, geography_id, year) "
        "VALUES (:sub, :geo, :yr) "
        "ON CONFLICT (subcategory_id, geography_id, year) DO NOTHING")
    created = 0
    with engine.begin() as conn:
        for sub in subs:
            for geo in geos:
                for yr in years:
                    res = conn.execute(insert_sql, {
                        "sub": sub["subcategory_id"],
                        "geo": geo["geography_id"],
                        "yr": yr,
                    })
                    created += res.rowcount or 0

    logger.info("build_cells: %d new cells (grid=%d, years=%s)", created, grid_size, years)
    return {"cells_created": created, "grid_size": grid_size,
            "anchor_year": anchor, "horizon_years": horizon, "years": years}


# --------------------------------------------------------------------------- #
# Stage 1 — ingest (pull verbatim payloads + normalise into raw_*; dedupe)
# --------------------------------------------------------------------------- #
def stage_ingest(engine: Engine, *, since: str | None = None) -> dict[str, Any]:
    """Pull verbatim payloads into the right ``raw_*`` table and normalise them.

    Each new payload is landed verbatim into ``raw_json`` (deduped by content
    hash) and its typed columns are filled in the same transaction, so a row
    arrives complete. Re-running inserts nothing new and writes nothing — the
    composite content-hash guard makes ingestion idempotent.
    """
    inserted = 0
    normalized = 0
    skipped_sources = 0
    with engine.connect() as conn:
        sources = _fetch_sources(conn, enabled_only=True)
        taxonomy = _fetch_taxonomy(conn)
        geographies = _fetch_geographies(conn)

    for src in sources:
        raw_table = src.get("raw_table")
        if raw_table not in ALLOWED_RAW_TABLES:
            logger.warning("source %s has unknown raw_table %r — skipping",
                           src["source_id"], raw_table)
            skipped_sources += 1
            continue
        cls = _resolve_connector_class(src.get("connector"))
        if cls is None:
            skipped_sources += 1
            continue
        try:
            connector = cls(src, _load_credential(src))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not construct connector for %s: %s",
                           src["source_id"], exc)
            skipped_sources += 1
            continue

        # Probe first — only pull when the source is reachable + authorised.
        try:
            probe = connector.probe()
            status = getattr(probe, "status", "UNREACHABLE")
            detail = getattr(probe, "detail", "")
        except Exception as exc:  # noqa: BLE001
            status, detail = "UNREACHABLE", str(exc)
        with engine.begin() as conn:
            _record_probe(conn, src["source_id"], status, detail)
        if status != "OK":
            logger.info("source %s not OK (%s) — skipping pull", src["source_id"], status)
            continue

        with engine.connect() as conn:
            typed_cols = _typed_columns(conn, raw_table)

        # Insert verbatim, deduped by content hash, RETURNING the new id so we
        # can normalise exactly the rows that were actually landed.
        ins_sql = text(
            f"INSERT INTO {raw_table} (source_id, raw_json) "
            f"SELECT :sid, CAST(:payload AS jsonb) "
            f"WHERE NOT EXISTS (SELECT 1 FROM {raw_table} "
            f"  WHERE source_id = :sid "
            f"  AND md5(raw_json::text) = md5(CAST(:payload AS jsonb)::text)) "
            f"RETURNING raw_id")
        n_new = 0
        n_norm = 0
        try:
            with engine.begin() as conn:
                for raw in connector.pull(taxonomy=taxonomy, geographies=geographies,
                                          since=since):
                    payload = json.dumps(raw, default=str, ensure_ascii=False)
                    new_row = conn.execute(
                        ins_sql, {"sid": src["source_id"], "payload": payload}).first()
                    if new_row is None:
                        continue  # duplicate payload — idempotent skip
                    n_new += 1
                    if not typed_cols:
                        continue
                    try:
                        norm = connector.normalize(raw)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("normalize-at-ingest failed (%s raw_id=%s): %s",
                                       src["source_id"], new_row.raw_id, exc)
                        continue
                    if _apply_normalized(conn, raw_table=raw_table,
                                         raw_id=new_row.raw_id,
                                         typed_cols=typed_cols, norm=norm):
                        n_norm += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("pull failed for %s: %s", src["source_id"], exc)
            continue
        inserted += n_new
        normalized += n_norm
        logger.info("ingested %d new rows (%d normalised) from %s",
                    n_new, n_norm, src["source_id"])

    return {"raw_rows_inserted": inserted, "raw_rows_normalized": normalized,
            "sources_skipped": skipped_sources}


# --------------------------------------------------------------------------- #
# Stage 2 — normalize (backfill typed columns for any un-normalised rows)
# --------------------------------------------------------------------------- #
def stage_normalize(engine: Engine) -> dict[str, Any]:
    """Backfill typed columns for raw rows that arrived un-normalised.

    ``ingest`` normalises rows as it lands them; this stage exists for rows that
    predate a connector's ``normalize`` implementation (or were imported by other
    means). A row is "un-normalised" when *every* typed column is NULL.
    """
    updated = 0
    with engine.connect() as conn:
        sources = _fetch_sources(conn, enabled_only=True)

    for src in sources:
        raw_table = src.get("raw_table")
        if raw_table not in ALLOWED_RAW_TABLES:
            continue
        cls = _resolve_connector_class(src.get("connector"))
        if cls is None:
            continue
        try:
            connector = cls(src, _load_credential(src))
        except Exception:  # noqa: BLE001
            continue

        with engine.begin() as conn:
            typed_cols = _typed_columns(conn, raw_table)
            if not typed_cols:
                continue
            # Un-normalised rows = every typed column still NULL.
            null_pred = " AND ".join(f'"{c}" IS NULL' for c in typed_cols)
            rows = conn.execute(text(
                f"SELECT raw_id, raw_json FROM {raw_table} "
                f"WHERE source_id = :sid AND ({null_pred})"),
                {"sid": src["source_id"]}).fetchall()

            for row in rows:
                try:
                    norm = connector.normalize(row.raw_json)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("normalize failed (%s raw_id=%s): %s",
                                   src["source_id"], row.raw_id, exc)
                    continue
                if _apply_normalized(conn, raw_table=raw_table, raw_id=row.raw_id,
                                     typed_cols=typed_cols, norm=norm):
                    updated += 1
        logger.info("normalized rows for %s", src["source_id"])

    return {"raw_rows_normalized": updated}


# --------------------------------------------------------------------------- #
# Stage 3 — size_cells (run applicable methods -> upsert cell_triangulation)
# --------------------------------------------------------------------------- #
def stage_size_cells(engine: Engine) -> dict[str, Any]:
    """Run every applicable method against every active cell; upsert estimates.

    A method is *applicable* when at least one of its ``required_raw_tables``
    currently holds data (methods with no declared inputs always run). Each
    method executes inside a SAVEPOINT so a failure isolates to that method and
    cannot roll back a cell's other estimates. Upserts are keyed on
    ``(cell_id, method_code, source_id)`` — re-running never duplicates a fact
    row, and the "no row without a source" invariant is enforced before write.
    """
    with engine.connect() as conn:
        method_codes = [r.method_code for r in conn.execute(text(
            "SELECT method_code FROM method_registry ORDER BY method_code"))]
        cells = [dict(r._mapping) for r in conn.execute(text(
            "SELECT c.cell_id, c.subcategory_id, c.geography_id, c.year, "
            "       s.name AS subcategory_name, s.hs_codes, s.regulatory_codes, "
            "       g.country, g.segment "
            "FROM cells c "
            "JOIN taxonomy_subcategories s ON s.subcategory_id = c.subcategory_id "
            "JOIN geographies g ON g.geography_id = c.geography_id "
            "WHERE c.status = 'active'"))]
        non_empty = _non_empty_raw_tables(conn)

    methods = {code: _resolve_method(code) for code in method_codes}
    methods = {c: m for c, m in methods.items() if m is not None}

    # Keep only methods whose inputs actually contain data (cheap global filter;
    # the method itself still returns [] for a cell with no matching rows).
    applicable: dict[str, Any] = {}
    for code, method in methods.items():
        required = list(getattr(method, "required_raw_tables", []) or [])
        if not required or any(t in non_empty for t in required):
            applicable[code] = method
        else:
            logger.info("method %s skipped — none of its raw tables %s hold data",
                        code, required)

    if not applicable:
        logger.warning("no applicable methods (registry empty or no raw data) — "
                       "size_cells is a no-op")
        return {"triangulation_upserts": 0, "cells_processed": len(cells),
                "methods_applicable": 0}

    upsert_sql = text(
        "INSERT INTO cell_triangulation "
        "(cell_id, method_code, estimate_usd_m, source_id, notes) "
        "VALUES (:cell_id, :method_code, :estimate_usd_m, :source_id, :notes) "
        "ON CONFLICT (cell_id, method_code, source_id) DO UPDATE SET "
        "estimate_usd_m = EXCLUDED.estimate_usd_m, notes = EXCLUDED.notes, "
        "computed_at = now()")

    upserts = 0
    dropped = 0
    for cell in cells:
        with engine.begin() as conn:
            for code, method in applicable.items():
                sp = conn.begin_nested()  # SAVEPOINT — isolate this method
                try:
                    results = method.estimate(cell, conn) or []
                    for r in results:
                        source_id = r.get("source_id")
                        estimate = r.get("estimate_usd_m")
                        if source_id is None or estimate is None:
                            # Invariant: no fact row without a source / value.
                            logger.warning("dropping estimate from %s (missing "
                                           "source_id or estimate) on cell %s",
                                           code, cell["cell_id"])
                            dropped += 1
                            continue
                        conn.execute(upsert_sql, {
                            "cell_id": cell["cell_id"],
                            "method_code": r.get("method_code", code),
                            "estimate_usd_m": estimate,
                            "source_id": source_id,
                            "notes": r.get("notes"),
                        })
                        upserts += 1
                    sp.commit()
                except Exception as exc:  # noqa: BLE001 — isolate per method
                    sp.rollback()
                    logger.warning("method %s failed on cell %s: %s",
                                   code, cell["cell_id"], exc)

    return {"triangulation_upserts": upserts, "cells_processed": len(cells),
            "methods_applicable": len(applicable), "estimates_dropped": dropped}


# --------------------------------------------------------------------------- #
# Stage 4 — score_confidence (refresh summary view; project onto cells, capped)
# --------------------------------------------------------------------------- #
def stage_score_confidence(engine: Engine) -> dict[str, Any]:
    """Refresh the summary view and project its verdict onto ``cells``.

    Confidence is computed ONLY by ``cell_triangulation_summary`` (no write-time
    human override). The view's HIGH/MEDIUM/LOW verdict is then **capped per
    method**: a cell's ceiling is the best ``confidence_cap`` among its
    contributing methods (NULL = uncapped). So ``web_search_extraction`` (capped
    LOW) can seed triangulation but can never, on its own, lift a cell above LOW —
    enforcing the spec invariant structurally, independent of the active profile.
    """
    # REFRESH ... CONCURRENTLY cannot run inside a transaction block; use an
    # autocommit connection and fall back to a plain refresh on first build.
    refreshed = "concurrent"
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        try:
            conn.execute(text(
                "REFRESH MATERIALIZED VIEW CONCURRENTLY cell_triangulation_summary"))
        except Exception as exc:  # noqa: BLE001 — likely never populated yet
            logger.info("concurrent refresh unavailable (%s); doing plain refresh", exc)
            conn.execute(text("REFRESH MATERIALIZED VIEW cell_triangulation_summary"))
            refreshed = "plain"

    # Project the view's verdict onto cells, applying the per-method cap. Ranks:
    # low=1, medium=2, high=3; the final confidence is LEAST(view, ceiling).
    update_sql = text(
        "WITH caps AS ( "
        "  SELECT ct.cell_id, "
        "         MAX(CASE mr.confidence_cap WHEN 'low' THEN 1 WHEN 'medium' THEN 2 "
        "                  WHEN 'high' THEN 3 ELSE 3 END) AS ceiling_rank "
        "  FROM cell_triangulation ct "
        "  JOIN method_registry mr ON mr.method_code = ct.method_code "
        "  GROUP BY ct.cell_id "
        "), "
        "verdict AS ( "
        "  SELECT s.cell_id, s.estimate_min, s.estimate_median, s.estimate_max, "
        "         s.n_distinct_methods, s.effective_signals, s.n_source_classes, "
        "         s.spread_ratio, "
        "         CASE WHEN s.qualifies_high THEN 3 WHEN s.qualifies_medium THEN 2 "
        "              ELSE 1 END AS view_rank "
        "  FROM cell_triangulation_summary s "
        ") "
        "UPDATE cells c SET "
        "  tam_revenue_usd_m = v.estimate_median, "
        "  tam_low_usd_m = v.estimate_min, "
        "  tam_high_usd_m = v.estimate_max, "
        "  confidence = CASE LEAST(v.view_rank, COALESCE(caps.ceiling_rank, v.view_rank)) "
        "                 WHEN 3 THEN 'high' WHEN 2 THEN 'medium' ELSE 'low' END, "
        "  confidence_rationale = format( "
        "    '%s distinct methods, %s independent signals, %s source classes, spread=%s%s', "
        "    v.n_distinct_methods, v.effective_signals, v.n_source_classes, "
        "    round(coalesce(v.spread_ratio, 0)::numeric, 3), "
        "    CASE WHEN caps.ceiling_rank IS NOT NULL AND caps.ceiling_rank < v.view_rank "
        "         THEN ' — capped at ' || (CASE caps.ceiling_rank WHEN 1 THEN 'low' "
        "                  WHEN 2 THEN 'medium' ELSE 'high' END) "
        "                  || ' by method confidence_cap' "
        "         ELSE '' END), "
        "  updated_at = now() "
        "FROM verdict v LEFT JOIN caps ON caps.cell_id = v.cell_id "
        "WHERE v.cell_id = c.cell_id")

    with engine.begin() as conn:
        updated = conn.execute(update_sql).rowcount
        capped = conn.execute(text(
            "SELECT count(*) FROM cells WHERE confidence_rationale LIKE "
            "'%by method confidence_cap%'")).scalar() or 0

    return {"view_refresh": refreshed, "cells_scored": updated or 0,
            "cells_capped": int(capped)}


# --------------------------------------------------------------------------- #
# Stage 5 — probe_health (re-probe every source; budget pre-warning + Slack)
# --------------------------------------------------------------------------- #
def stage_probe_health(engine: Engine) -> dict[str, Any]:
    """Re-probe every source into the 7-state taxonomy and surface warnings.

    Persists ``last_probe_*`` on ``sources``, emits a Slack alert for unhealthy
    connectors, and a 🟠 budget pre-warning at ~80% of a source's
    ``quota_ceiling`` (using landed raw-row count as the usage proxy, since v1
    has no per-call spend ledger).
    """
    counts: dict[str, int] = {}
    failures: list[tuple[str, str, str]] = []
    budget_warnings: list[tuple[str, str]] = []
    with engine.connect() as conn:
        sources = _fetch_sources(conn, enabled_only=False)

    for src in sources:
        cls = _resolve_connector_class(src.get("connector"))
        connector = None
        if cls is None:
            status, detail = "UNREACHABLE", "no connector module registered"
        else:
            try:
                connector = cls(src, _load_credential(src))
                probe = connector.probe()
                status = getattr(probe, "status", "UNREACHABLE")
                detail = getattr(probe, "detail", "")
            except Exception as exc:  # noqa: BLE001
                status, detail = "UNREACHABLE", str(exc)

        with engine.begin() as conn:
            _record_probe(conn, src["source_id"], status, detail)
        counts[status] = counts.get(status, 0) + 1
        if status != "OK":
            failures.append((src["source_id"], status, detail or ""))

        warning = _budget_prewarning(engine, src, connector)
        if warning:
            budget_warnings.append((src["source_id"], warning))

    _notify_failures(failures)
    _notify_budget(budget_warnings)
    return {"probe_status_counts": counts, "unhealthy": len(failures),
            "budget_prewarnings": [f"{sid}: {msg}" for sid, msg in budget_warnings]}


def _budget_prewarning(engine: Engine, src: dict[str, Any], connector: Any) -> str | None:
    """Compute a 🟠 budget/quota pre-warning for one source (or ``None``).

    Usage is proxied by the count of landed raw rows for the source (one row ≈
    one retrieved record/call); compared against ``quota_ceiling`` at the spec's
    ~80% threshold. Delegates the actual thresholding to the connector's
    ``budget_warning`` helper when available, falling back to an inline check.
    """
    ceiling = src.get("quota_ceiling")
    raw_table = src.get("raw_table")
    if not ceiling or raw_table not in ALLOWED_RAW_TABLES:
        return None
    try:
        with engine.connect() as conn:
            used = conn.execute(text(
                f"SELECT count(*) FROM {raw_table} WHERE source_id = :sid"),
                {"sid": src["source_id"]}).scalar() or 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("usage proxy query failed for %s: %s", src["source_id"], exc)
        return None

    if connector is not None and hasattr(connector, "budget_warning"):
        try:
            return connector.budget_warning(used_calls=int(used))
        except Exception:  # noqa: BLE001
            pass
    if int(used) >= 0.8 * int(ceiling):
        return f"{used} of {int(ceiling)} calls"
    return None


def _notify_failures(failures: list[tuple[str, str, str]]) -> None:
    """Best-effort Slack notification of unhealthy connectors (Q7)."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook or not failures:
        return
    lines = "\n".join(f"• `{sid}` → *{st}* — {det[:160]}" for sid, st, det in failures)
    _post_slack(webhook, f":rotating_light: {len(failures)} connector(s) unhealthy:\n{lines}")


def _notify_budget(warnings: list[tuple[str, str]]) -> None:
    """Best-effort Slack 🟠 pre-warning when sources near their quota ceiling."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook or not warnings:
        return
    lines = "\n".join(f"• `{sid}` — {msg}" for sid, msg in warnings)
    _post_slack(webhook, f":large_orange_circle: {len(warnings)} source(s) nearing "
                         f"budget/quota:\n{lines}")


def _post_slack(webhook: str, text_body: str) -> None:
    try:
        import httpx  # local import: optional dependency at notify time
    except Exception:  # noqa: BLE001
        return
    try:
        httpx.post(webhook, json={"text": text_body}, timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Slack notify failed: %s", exc)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
STAGES: dict[str, Callable[..., dict[str, Any]]] = {
    "build_cells": stage_build_cells,
    "ingest": stage_ingest,
    "normalize": stage_normalize,
    "size_cells": stage_size_cells,
    "score_confidence": stage_score_confidence,
    "probe_health": stage_probe_health,
}


def run(
    stages: list[str] | None = None,
    *,
    since: str | None = None,
    anchor_year: int | None = None,
    horizon_years: int | None = None,
) -> dict[str, Any]:
    """Run the requested stages in canonical order and return a summary report."""
    selected = stages or STAGE_ORDER
    # Always execute in the canonical dependency order regardless of input order.
    selected = [s for s in STAGE_ORDER if s in selected]
    engine = get_engine()
    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {"started_at": started.isoformat(), "stages": {}}

    for name in selected:
        stage_started = datetime.now(timezone.utc)
        logger.info("=== stage %s starting ===", name)
        try:
            if name == "ingest":
                result = STAGES[name](engine, since=since)
            elif name == "build_cells":
                result = STAGES[name](engine, anchor_year=anchor_year,
                                      horizon_years=horizon_years)
            else:
                result = STAGES[name](engine)
            report["stages"][name] = {
                "ok": True,
                "duration_s": (datetime.now(timezone.utc) - stage_started).total_seconds(),
                **result,
            }
            logger.info("=== stage %s done: %s ===", name, result)
        except Exception as exc:  # noqa: BLE001 — isolate stage failures
            logger.exception("stage %s failed", name)
            report["stages"][name] = {"ok": False, "error": str(exc)}

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["all_ok"] = all(s.get("ok") for s in report["stages"].values())
    return report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="GRX10 market-research pipeline runner")
    parser.add_argument(
        "--stages", default=",".join(STAGE_ORDER),
        help="comma-separated subset of: " + ", ".join(STAGE_ORDER))
    parser.add_argument(
        "--since", default=os.environ.get("PIPELINE_SINCE"),
        help="ingestion lookback hint (ISO date) passed to connector.pull(since=...)")
    parser.add_argument(
        "--anchor-year", type=int, default=None,
        help="base year for the cell grid (default: env PIPELINE_ANCHOR_YEAR or last complete year)")
    parser.add_argument(
        "--horizon-years", type=int, default=None,
        help="forecast years beyond the anchor (default: env PIPELINE_HORIZON_YEARS or 0)")
    args = parser.parse_args(argv)

    requested = [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = [s for s in requested if s not in STAGES]
    if unknown:
        parser.error(f"unknown stage(s): {', '.join(unknown)}")

    report = run(requested, since=args.since, anchor_year=args.anchor_year,
                 horizon_years=args.horizon_years)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
