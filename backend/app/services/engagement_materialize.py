"""Materialize a new engagement from a confirmed brief plan.

Turns the (user-edited) brief blueprint into real rows: an ``engagements`` row,
its taxonomy (families + subcategories with HS/regulatory crosswalks),
geographies, per-engagement namespaced sources (generic-REST config where the
brief supplied a candidate endpoint), and a capped grid of ``planned`` cells
(subcategory × geography × the two anchor years). The web-search auto-seed that
puts LOW-confidence numbers on those planned cells is a SEPARATE step
(``services.seed_job``) launched after the cost-confirm gate.

Design decisions (docs/MULTI_ENGAGEMENT_PLAN.md):
* Sources are per-engagement rows with a namespaced ``source_id``
  (``<engagement_id>__<base>``) — single-column PK, no raw-FK ripple.
* Cell grid is capped at :data:`MAX_CELLS`; the clamp is reported back so the UI
  can surface it and the cost banner is honest.
* Proposed connectors (new-vertical sources with no Python connector class) are
  created with a generic-REST ``config`` from the brief's candidate endpoint and
  marked ``proposed`` — a probe/onboarding step (or web-search) covers them; we
  do NOT block engagement creation on live network probes.

IDs: ``family_id`` / ``subcategory_id`` / ``geography_id`` are manually assigned
(``MAX(id)+1`` within the create transaction — creates are effectively
serialized in this single-operator tool); ``cell_id`` is serial.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("grx10.services.engagement_materialize")

MAX_CELLS = 120           # hard ceiling on the materialized grid (cost guardrail)
DEFAULT_SEGMENT = "DOMESTIC"


# ─── helpers ──────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:40] or "engagement"


def _unique_engagement_id(session: Session, name: str) -> str:
    base = f"eng_{_slug(name)}"
    candidate = base
    n = 1
    while session.execute(
        text("SELECT 1 FROM engagements WHERE engagement_id = :e"), {"e": candidate}
    ).first():
        n += 1
        candidate = f"{base}_{n}"
    return candidate


def _next_id(session: Session, table: str, col: str) -> int:
    return int(
        session.execute(text(f"SELECT COALESCE(MAX({col}), 0) + 1 FROM {table}")).scalar_one()
    )


def _anchor_years(year_from: int, year_to: int) -> list[int]:
    """The two anchor years (dedup if equal) — cells only for these, not the range."""
    yrs = sorted({int(year_from), int(year_to)})
    return yrs


# ─── main ─────────────────────────────────────────────────────────────────────

def materialize_engagement(
    session: Session,
    *,
    name: str,
    brief_text: str | None,
    geographies: list[str],
    year_from: int,
    year_to: int,
    plan: dict[str, Any] | None,
    families: list[str] | None = None,
) -> dict[str, Any]:
    """Create a full engagement from a confirmed brief plan and return a summary.

    Runs in the caller's transaction (the router commits). Returns a dict with
    the new ``engagement_id`` and counts + the clamp flag for the cost banner.
    """
    plan = plan or {}
    families = families or plan.get("families") or []
    proposed_subcats: list[dict[str, Any]] = plan.get("proposed_subcategories") or []
    connector_plan: list[dict[str, Any]] = plan.get("connector_plan") or []
    web_search_enabled = bool(plan.get("web_search_enabled", True))

    engagement_id = _unique_engagement_id(session, name)
    session.execute(
        text(
            "INSERT INTO engagements (engagement_id, name, is_demo, status, "
            "  active_profile, web_search_enabled, brief_text) "
            "VALUES (:id, :name, false, 'active', 'Standard', :ws, :bt)"
        ),
        {"id": engagement_id, "name": name, "ws": web_search_enabled, "bt": brief_text},
    )

    # 1 — Taxonomy (families + subcategories) --------------------------------
    fam_ids: dict[str, int] = {}
    subcats: list[tuple[int, str]] = []  # (subcategory_id, name) for grid building

    if proposed_subcats:
        # New vertical: build taxonomy from the LLM's proposal.
        fam_names = [f for f in families] or sorted({s["family"] for s in proposed_subcats})
        for fam in fam_names:
            fid = _next_id(session, "taxonomy_families", "family_id")
            session.execute(
                text(
                    "INSERT INTO taxonomy_families (family_id, name, version, engagement_id) "
                    "VALUES (:fid, :name, 1, :eng)"
                ),
                {"fid": fid, "name": fam, "eng": engagement_id},
            )
            fam_ids[fam] = fid
        for sc in proposed_subcats:
            fam = sc.get("family") or (fam_names[0] if fam_names else "General")
            fid = fam_ids.get(fam)
            if fid is None:  # subcategory referencing an unlisted family — create it
                fid = _next_id(session, "taxonomy_families", "family_id")
                session.execute(
                    text(
                        "INSERT INTO taxonomy_families (family_id, name, version, engagement_id) "
                        "VALUES (:fid, :name, 1, :eng)"
                    ),
                    {"fid": fid, "name": fam, "eng": engagement_id},
                )
                fam_ids[fam] = fid
            sid = _next_id(session, "taxonomy_subcategories", "subcategory_id")
            session.execute(
                text(
                    "INSERT INTO taxonomy_subcategories "
                    "(subcategory_id, family_id, name, hs_codes, regulatory_codes, version, engagement_id) "
                    "VALUES (:sid, :fid, :name, :hs, :reg, 1, :eng)"
                ),
                {
                    "sid": sid, "fid": fid, "name": sc.get("name", "General"),
                    "hs": sc.get("hs_codes") or [],
                    "reg": sc.get("regulatory_codes") or [],
                    "eng": engagement_id,
                },
            )
            subcats.append((sid, sc.get("name", "General")))
    else:
        # In-catalog: clone the selected families' taxonomy from the default engagement.
        rows = session.execute(
            text(
                "SELECT f.name AS fam, sc.name AS sub, sc.hs_codes, sc.regulatory_codes "
                "FROM taxonomy_subcategories sc "
                "JOIN taxonomy_families f ON f.family_id = sc.family_id "
                "WHERE f.engagement_id = 'eng_medtech' "
                "  AND (:all_fams OR f.name = ANY(:fams)) "
                "ORDER BY f.family_id, sc.subcategory_id"
            ),
            {"all_fams": not families, "fams": families or [""]},
        ).mappings().all()
        for r in rows:
            fam = r["fam"]
            if fam not in fam_ids:
                fid = _next_id(session, "taxonomy_families", "family_id")
                session.execute(
                    text(
                        "INSERT INTO taxonomy_families (family_id, name, version, engagement_id) "
                        "VALUES (:fid, :name, 1, :eng)"
                    ),
                    {"fid": fid, "name": fam, "eng": engagement_id},
                )
                fam_ids[fam] = fid
            sid = _next_id(session, "taxonomy_subcategories", "subcategory_id")
            session.execute(
                text(
                    "INSERT INTO taxonomy_subcategories "
                    "(subcategory_id, family_id, name, hs_codes, regulatory_codes, version, engagement_id) "
                    "VALUES (:sid, :fid, :name, :hs, :reg, 1, :eng)"
                ),
                {
                    "sid": sid, "fid": fam_ids[fam], "name": r["sub"],
                    "hs": list(r["hs_codes"] or []), "reg": list(r["regulatory_codes"] or []),
                    "eng": engagement_id,
                },
            )
            subcats.append((sid, r["sub"]))

    # 2 — Geographies --------------------------------------------------------
    geo_ids: list[int] = []
    for country in geographies or []:
        gid = _next_id(session, "geographies", "geography_id")
        session.execute(
            text(
                "INSERT INTO geographies (geography_id, country, segment, engagement_id) "
                "VALUES (:gid, :country, :seg, :eng)"
            ),
            {"gid": gid, "country": country, "seg": DEFAULT_SEGMENT, "eng": engagement_id},
        )
        geo_ids.append(gid)

    # 3 — Sources (namespaced, generic-REST config for proposed connectors) --
    source_count = 0
    seen_sids: set[str] = set()
    for c in connector_plan:
        base = c.get("source_id") or _slug(c.get("publisher", "source"))
        # Strip any pre-existing engagement prefix, then namespace to THIS engagement.
        base = base.split("__", 1)[-1]
        sid = f"{engagement_id}__{base}"
        if sid in seen_sids:
            continue
        seen_sids.add(sid)
        base_url = (c.get("base_url") or "").strip()
        endpoint_path = (c.get("endpoint_path") or "").strip()
        auth = (c.get("auth_type") or "none").strip() or "none"
        is_proposed = "proposed" in (c.get("status", "").lower()) or bool(base_url)
        # Generic-REST config lives in sources.notes as JSON (matches the custom-REST
        # convention in connectors.create_custom_source); base URL goes in url_pattern.
        note_label = ("Proposed connector — needs onboarding/probe."
                      if is_proposed else "From confirmed brief plan.")
        if base_url:
            notes_str = json.dumps({
                "endpoints": [{"name": "data", "path": endpoint_path or "/"}],
                "field_map": [],
                "user_notes": note_label,
            })
        else:
            notes_str = note_label
        session.execute(
            text(
                "INSERT INTO sources (source_id, publisher, url_pattern, auth, class, "
                "  connector, raw_table, access_method, enabled, engagement_id, notes) "
                "VALUES (:sid, :pub, :url, :auth, :cls, :conn, :raw, :access, :enabled, :eng, :notes) "
                "ON CONFLICT (source_id) DO NOTHING"
            ),
            {
                "sid": sid, "pub": c.get("publisher", base), "url": base_url or None,
                "auth": auth, "cls": c.get("source_class", "B"),
                "conn": "generic_rest" if base_url else None,
                "raw": c.get("raw_table") or "raw_news",
                "access": "api",
                "enabled": not is_proposed,
                "eng": engagement_id,
                "notes": notes_str,
            },
        )
        source_count += 1

    # Always ensure a web-search source exists (LOW-capped fallback that seeds cells).
    ws_sid = f"{engagement_id}__web_search"
    session.execute(
        text(
            "INSERT INTO sources (source_id, publisher, auth, class, raw_table, "
            "  access_method, enabled, engagement_id, notes) "
            "VALUES (:sid, 'Web search (LOW-capped fallback)', 'none', 'C', 'raw_news', "
            "  'web_search', :ws, :eng, 'Auto-registered web-search fallback.') "
            "ON CONFLICT (source_id) DO NOTHING"
        ),
        {"sid": ws_sid, "ws": web_search_enabled, "eng": engagement_id},
    )

    # 4 — Cell grid (subcategory × geography × anchor years), capped ---------
    years = _anchor_years(year_from, year_to)
    planned = 0
    capped = False
    for sid, _name in subcats:
        for gid in geo_ids:
            for yr in years:
                if planned >= MAX_CELLS:
                    capped = True
                    break
                session.execute(
                    text(
                        "INSERT INTO cells (subcategory_id, geography_id, year, status, engagement_id) "
                        "VALUES (:sid, :gid, :yr, 'planned', :eng) "
                        "ON CONFLICT (engagement_id, subcategory_id, geography_id, year) DO NOTHING"
                    ),
                    {"sid": sid, "gid": gid, "yr": yr, "eng": engagement_id},
                )
                planned += 1
            if capped:
                break
        if capped:
            break

    logger.info(
        "materialized %s: %d families, %d subcats, %d geos, %d sources, %d planned cells (capped=%s)",
        engagement_id, len(fam_ids), len(subcats), len(geo_ids), source_count, planned, capped,
    )
    return {
        "engagement_id": engagement_id,
        "name": name,
        "families": len(fam_ids),
        "subcategories": len(subcats),
        "geographies": len(geo_ids),
        "sources": source_count,
        "planned_cells": planned,
        "capped": capped,
        "web_search_enabled": web_search_enabled,
    }
