"""Web-search auto-seed for a new engagement's ``planned`` cells.

Puts a first-draft LOW-confidence TAM (with a real, drillable source URL) on each
planned cell using the Anthropic ``web_search`` tool — no per-source connector or
credential needed, so a brand-new vertical engagement is browsable immediately.

Two run modes (chosen by environment), same work either way:
* **Render** (``RENDER_API_KEY`` + a pipeline service): POST a one-off Job scoped
  to ``ENGAGEMENT_ID`` so the ~N searches run on an isolated dyno with retries,
  independent of the web request. (Falls back to local if the Job API call fails.)
* **Local / no Render**: run in-process via FastAPI ``BackgroundTasks`` — bounded
  so a dev box isn't hammered.

Each cell flips ``planned → active`` in the DB as it's sized, so the dashboard
fills progressively on its normal refetch — no separate progress channel.

If ``ANTHROPIC_API_KEY`` is absent the seed is a no-op with a clear detail
(cells stay ``planned`` — honest "awaiting data").
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
from typing import Any

import httpx
from sqlalchemy import text

from backend.app.config import settings
from backend.app.db import get_session

logger = logging.getLogger("grx10.services.seed_job")

# Bound the in-process seed so a box isn't hammered by agentic searches, and run
# them concurrently so a full engagement seeds in minutes, not an hour.
LOCAL_SEED_CAP = 200
SEED_WORKERS = 8
_MODEL = "claude-sonnet-4-6"
_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
_RENDER_PIPELINE_SERVICE = os.environ.get("RENDER_PIPELINE_SERVICE_ID", "")


# ─── entry point ──────────────────────────────────────────────────────────────

def launch_seed(engagement_id: str, *, background: Any = None) -> dict[str, Any]:
    """Decide the run mode and launch. Returns {launched, mode, detail}."""
    if not settings.ANTHROPIC_API_KEY:
        return {"launched": False, "mode": "disabled",
                "detail": "ANTHROPIC_API_KEY not set — cells stay 'planned' (no web search)."}

    # Render one-off Job path (best-effort; falls back to local on any failure).
    render_key = os.environ.get("RENDER_API_KEY", "")
    if render_key and _RENDER_PIPELINE_SERVICE:
        try:
            _launch_render_job(engagement_id, render_key)
            return {"launched": True, "mode": "render_job",
                    "detail": "Launched a Render one-off Job to seed all planned cells."}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Render job launch failed (%s); falling back to local seed", exc)

    if background is not None:
        background.add_task(run_seed, engagement_id, LOCAL_SEED_CAP)
        return {"launched": True, "mode": "local_background",
                "detail": f"Seeding up to {LOCAL_SEED_CAP} cells in the background (web search)."}
    # Synchronous fallback (no BackgroundTasks handle).
    run_seed(engagement_id, LOCAL_SEED_CAP)
    return {"launched": True, "mode": "local_sync", "detail": "Seeded cells synchronously."}


def _launch_render_job(engagement_id: str, render_key: str) -> None:
    """POST a one-off Render Job that runs the pipeline seed for this engagement."""
    resp = httpx.post(
        f"https://api.render.com/v1/services/{_RENDER_PIPELINE_SERVICE}/jobs",
        headers={"Authorization": f"Bearer {render_key}", "Content-Type": "application/json"},
        json={
            "startCommand": "python -m pipeline.run --engagement "
                            f"{engagement_id} --stages size_cells,score_confidence --web-search-only",
        },
        timeout=30.0,
    )
    resp.raise_for_status()


# ─── the seed itself ──────────────────────────────────────────────────────────

def run_seed(engagement_id: str, cap: int) -> dict[str, Any]:
    """Web-search-size up to *cap* planned cells for the engagement.

    Runs the per-cell agentic searches CONCURRENTLY (bounded pool) so a 60-cell
    engagement seeds in a few minutes instead of ~an hour serial. Only ``planned``
    cells are touched, so the job is idempotent + RESUMABLE — re-running it (e.g.
    after a dyno restart killed a prior run) simply continues the remaining cells.
    """
    session = next(get_session())
    try:
        cells = [dict(r) for r in session.execute(
            text(
                "SELECT c.cell_id, sc.name AS subcat, g.country, c.year "
                "FROM cells c "
                "JOIN taxonomy_subcategories sc ON sc.subcategory_id = c.subcategory_id "
                "JOIN geographies g ON g.geography_id = c.geography_id "
                "WHERE c.engagement_id = :e AND c.status = 'planned' "
                "ORDER BY c.year, c.cell_id LIMIT :cap"
            ),
            {"e": engagement_id, "cap": cap},
        ).mappings().all()]
    finally:
        session.close()

    ws_source = f"{engagement_id}__web_search"
    total = len(cells)
    sized = 0
    logger.info("seed starting for %s: %d planned cells, %d workers",
                engagement_id, total, SEED_WORKERS)

    with concurrent.futures.ThreadPoolExecutor(max_workers=SEED_WORKERS) as pool:
        futures = {
            pool.submit(_seed_one_cell, engagement_id, ws_source, c): c
            for c in cells
        }
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            try:
                if fut.result():
                    sized += 1
            except Exception as exc:  # noqa: BLE001 — one cell never stops the batch
                logger.warning("seed cell failed: %s", exc)
            if done % 10 == 0 or done == total:
                logger.info("seed progress %s: %d/%d done, %d sized",
                            engagement_id, done, total, sized)

    # Refresh the confidence view once, after the batch.
    try:
        refresh_session = next(get_session())
        try:
            with refresh_session.connection().engine.connect() as raw:
                raw.execution_options(isolation_level="AUTOCOMMIT").execute(
                    text("REFRESH MATERIALIZED VIEW cell_triangulation_summary"))
        finally:
            refresh_session.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("matview refresh after seed failed: %s", exc)

    logger.info("seed complete for %s: %d/%d cells sized", engagement_id, sized, total)
    return {"engagement_id": engagement_id, "sized": sized, "total": total}


def _seed_one_cell(engagement_id: str, ws_source: str, c: dict) -> bool:
    """Size one planned cell via web search, in its own DB session (thread-safe)."""
    try:
        usd_m, url, snippet = _size_cell_via_websearch(c["subcat"], c["country"], c["year"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("cell %s web-search failed: %s", c["cell_id"], exc)
        return False
    if usd_m is None:
        return False
    session = next(get_session())
    try:
        _write_cell_estimate(session, engagement_id, ws_source, c["cell_id"], usd_m, url, snippet)
        session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.warning("cell %s write failed: %s", c["cell_id"], exc)
        return False
    finally:
        session.close()


def _size_cell_via_websearch(subcat: str, country: str, year: int) -> tuple[float | None, str | None, str | None]:
    """One agentic web search → (market_size_usd_millions, source_url, snippet)."""
    prompt = (
        f"Estimate the market size (USD millions) of '{subcat}' in {country} for {year}. "
        "First search the web for a published figure. If you find one, use it. If NO clean "
        "published figure exists for this exact segment, DO NOT return null — instead give your "
        "best-reasoned estimate derived from adjacent data (the parent/total market, comparable "
        "countries, typical segment share, and growth rates), and say so. Always return a number.\n"
        "Return ONLY a JSON object, no prose:\n"
        '{"market_size_usd_millions": <number, never null>, "basis": "<published|estimated>", '
        '"source_url": "<url, or the closest supporting source>", '
        '"snippet": "<the figure with its source, or a one-line note on how you estimated it>", '
        '"year_found": <int>}'
    )
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": settings.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": _MODEL,
            "max_tokens": 1024,
            "tools": [_SEARCH_TOOL],
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90.0,
    )
    resp.raise_for_status()
    # Concatenate text blocks (the model may interleave tool use + text).
    parts = [b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text"]
    raw = "\n".join(parts).strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None, None, None
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None, None, None
    usd = data.get("market_size_usd_millions")
    if usd is None:
        return None, data.get("source_url"), data.get("snippet")
    try:
        usd = float(usd)
    except (TypeError, ValueError):
        return None, data.get("source_url"), data.get("snippet")
    basis = (data.get("basis") or "").lower()
    snippet = (data.get("snippet") or "")[:480]
    if basis == "estimated":
        snippet = f"[modeled estimate] {snippet}"
    return usd, data.get("source_url"), snippet


def _write_cell_estimate(
    session, engagement_id: str, ws_source: str, cell_id: int,
    usd_m: float, url: str | None, snippet: str | None,
) -> None:
    """Land a raw_news row + a LOW web_search_extraction estimate + update the cell."""
    band = usd_m * 0.35  # LOW-tier wide band (±35%)
    # raw evidence
    session.execute(
        text(
            "INSERT INTO raw_news (source_id, engagement_id, raw_json) "
            "VALUES (:src, :eng, CAST(:rj AS jsonb))"
        ),
        {"src": ws_source, "eng": engagement_id,
         "rj": json.dumps({"url": url, "snippet": snippet, "market_size_usd_m": usd_m})},
    )
    # triangulation estimate (LOW cap via the web_search_extraction method)
    session.execute(
        text(
            "INSERT INTO cell_triangulation (cell_id, method_code, estimate_usd_m, source_id, "
            "  engagement_id, notes) "
            "VALUES (:cid, 'web_search_extraction', :est, :src, :eng, :notes) "
            "ON CONFLICT (cell_id, method_code, source_id) DO UPDATE SET estimate_usd_m = EXCLUDED.estimate_usd_m"
        ),
        {"cid": cell_id, "est": round(usd_m, 2), "src": ws_source, "eng": engagement_id,
         "notes": f"web_search_extraction (LOW): {url or 'no url'}"},
    )
    # cell TAM + LOW confidence + active
    session.execute(
        text(
            "UPDATE cells SET tam_revenue_usd_m = :mid, tam_low_usd_m = :lo, tam_high_usd_m = :hi, "
            "  confidence = 'low', status = 'active', "
            "  confidence_rationale = 'Single LOW-capped web-search estimate; add connectors to raise confidence.' "
            "WHERE cell_id = :cid AND engagement_id = :eng"
        ),
        {"mid": round(usd_m, 2), "lo": round(usd_m - band, 2), "hi": round(usd_m + band, 2),
         "cid": cell_id, "eng": engagement_id},
    )
