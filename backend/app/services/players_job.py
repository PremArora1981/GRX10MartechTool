"""AI player/competitor discovery for an engagement.

Populates the Players screen for ANY vertical: for each subcategory × geography,
one agentic call identifies the top companies + estimated shares, and we write
``companies`` + ``player_shares`` (LOW confidence, web-search sourced) for the
cells of that segment. Concurrent + idempotent (skips segments that already have
shares) so it's resumable, mirroring the seed.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging

import httpx
from sqlalchemy import text

from backend.app.config import settings
from backend.app.db import get_session

logger = logging.getLogger("grx10.services.players_job")

PLAYER_WORKERS = 6
_MODEL = "claude-sonnet-4-6"
_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}


def launch_players(engagement_id: str, *, background=None) -> dict:
    if not settings.ANTHROPIC_API_KEY:
        return {"launched": False, "mode": "disabled",
                "detail": "ANTHROPIC_API_KEY not set — player discovery needs it."}
    if background is not None:
        background.add_task(run_player_discovery, engagement_id)
        return {"launched": True, "mode": "local_background",
                "detail": "Discovering top players per segment in the background."}
    run_player_discovery(engagement_id)
    return {"launched": True, "mode": "local_sync", "detail": "Player discovery done."}


def run_player_discovery(engagement_id: str) -> dict:
    """Discover players for each subcategory×geography segment lacking shares."""
    session = next(get_session())
    try:
        segments = [dict(r) for r in session.execute(
            text(
                "SELECT c.subcategory_id, c.geography_id, sc.name AS subcat, g.country, "
                "       MIN(sc.family_id) AS family_id "
                "FROM cells c "
                "JOIN taxonomy_subcategories sc ON sc.subcategory_id = c.subcategory_id "
                "JOIN geographies g ON g.geography_id = c.geography_id "
                "WHERE c.engagement_id = :e "
                "  AND NOT EXISTS (SELECT 1 FROM player_shares ps "
                "                  WHERE ps.engagement_id = :e AND ps.cell_id IN "
                "                  (SELECT cell_id FROM cells c2 WHERE c2.engagement_id=:e "
                "                   AND c2.subcategory_id=c.subcategory_id "
                "                   AND c2.geography_id=c.geography_id)) "
                "GROUP BY c.subcategory_id, c.geography_id, sc.name, g.country"
            ),
            {"e": engagement_id},
        ).mappings().all()]
    finally:
        session.close()

    ws_source = f"{engagement_id}__web_search"
    filled = 0
    logger.info("player discovery for %s: %d segments, %d workers",
                engagement_id, len(segments), PLAYER_WORKERS)
    with concurrent.futures.ThreadPoolExecutor(max_workers=PLAYER_WORKERS) as pool:
        futures = [pool.submit(_discover_segment, engagement_id, ws_source, s) for s in segments]
        for fut in concurrent.futures.as_completed(futures):
            try:
                if fut.result():
                    filled += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("player segment failed: %s", exc)
    logger.info("player discovery complete for %s: %d/%d segments filled",
                engagement_id, filled, len(segments))
    return {"engagement_id": engagement_id, "segments_filled": filled, "segments": len(segments)}


def _discover_segment(engagement_id: str, ws_source: str, seg: dict) -> bool:
    players = _llm_players(seg["subcat"], seg["country"])
    if not players:
        return False
    session = next(get_session())
    try:
        # Cells in this segment (all years) + their TAM for revenue estimates.
        cells = session.execute(
            text("SELECT cell_id, tam_revenue_usd_m FROM cells "
                 "WHERE engagement_id=:e AND subcategory_id=:s AND geography_id=:g"),
            {"e": engagement_id, "s": seg["subcategory_id"], "g": seg["geography_id"]},
        ).all()
        for rank, p in enumerate(players[:6], start=1):
            name = (p.get("name") or "").strip()
            if not name:
                continue
            share = p.get("share_pct")
            try:
                share = float(share) if share is not None else None
            except (TypeError, ValueError):
                share = None
            cid = session.execute(
                text("SELECT company_id FROM companies WHERE engagement_id=:e AND name=:n"),
                {"e": engagement_id, "n": name},
            ).scalar()
            if cid is None:
                cid = session.execute(
                    text("INSERT INTO companies (name, company_type, country_hq, seeded_role, "
                         "  discovered, engagement_id) "
                         "VALUES (:n,'producer',:hq,'producer',true,:e) RETURNING company_id"),
                    {"n": name, "hq": p.get("hq") or seg["country"], "e": engagement_id},
                ).scalar_one()
            for (cell_id, tam) in cells:
                rev = round(float(tam) * share / 100.0, 1) if (tam and share) else None
                band = round(share * 0.2, 1) if share else None
                session.execute(
                    text(
                        "INSERT INTO player_shares (cell_id, company_id, player_role, rank, "
                        "  share_pct, share_low_pct, share_high_pct, revenue_usd_m, source_id, "
                        "  confidence, engagement_id) "
                        "VALUES (:cid,:co,'producer',:rk,:sh,:lo,:hi,:rev,:src,'low',:e) "
                        "ON CONFLICT (cell_id, company_id, player_role) DO NOTHING"
                    ),
                    {"cid": cell_id, "co": cid, "rk": rank, "sh": share,
                     "lo": (share - band) if (share and band) else None,
                     "hi": (share + band) if (share and band) else None,
                     "rev": rev, "src": ws_source, "e": engagement_id},
                )
        session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.warning("segment write failed (%s/%s): %s", seg["subcat"], seg["country"], exc)
        return False
    finally:
        session.close()


def _llm_players(subcat: str, country: str) -> list[dict]:
    prompt = (
        f"Identify the top 3-6 companies (market leaders) in the '{subcat}' market in "
        f"{country}, with each one's estimated market share percentage. Use web search for "
        "current data. Return ONLY a JSON array, no prose:\n"
        '[{"name":"<company>","share_pct":<number or null>,"hq":"<HQ country>"}]'
    )
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": settings.ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": _MODEL, "max_tokens": 1024, "tools": [_SEARCH_TOOL],
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90.0,
    )
    resp.raise_for_status()
    raw = "".join(b.get("text", "") for b in resp.json().get("content", [])
                  if b.get("type") == "text").strip()
    import re
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []
