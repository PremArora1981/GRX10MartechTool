"""Cell-level guided actions — ``/cells/{cell_id}/...``.

Turns "this cell is empty / low-confidence, now what?" into a concrete path:

  POST /cells/{cell_id}/suggest-sources
      Ask the LLM, for THIS exact subcategory × geography × year, which specific
      data sources would size it (publisher, tier, candidate endpoint, why) plus a
      one-line diagnosis of the current state. Grounded in what already feeds the
      cell so it doesn't re-suggest existing sources.

  POST /cells/{cell_id}/add-suggested-source
      Materialize one suggestion as a real engagement source (namespaced,
      generic-REST config) so the user can add a key on the Connectors page and
      "Pull data now". Returns the created source_id.

Both are engagement-scoped. suggest-sources needs ``ANTHROPIC_API_KEY``; without
it a deterministic fallback returns the generic source archetypes by raw table.
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.app.config import settings
from backend.app.deps import CurrentUserDep, DbSession, EngagementDep

logger = logging.getLogger("grx10.routers.cell_actions")

router = APIRouter(prefix="/cells", tags=["cell-actions"])

_MODEL = "claude-sonnet-4-6"
_RAW_TABLES = [
    "raw_trade_flows", "raw_filings", "raw_regulatory", "raw_industry_reports",
    "raw_external_metrics", "raw_procurement", "raw_patents", "raw_news",
]


class SuggestedSource(BaseModel):
    publisher: str
    source_class: str            # A | B | C
    raw_table: str
    base_url: str = ""
    endpoint_path: str = ""
    auth_type: str = "none"      # none | api_key | subscription
    why: str = ""


class SuggestSourcesOut(BaseModel):
    cell_id: int
    subcategory: str
    country: str
    year: int
    diagnosis: str
    existing_sources: list[str]
    suggestions: list[SuggestedSource] = Field(default_factory=list)


class AddSuggestedIn(BaseModel):
    publisher: str
    source_class: str = "B"
    raw_table: str = "raw_news"
    base_url: str = ""
    endpoint_path: str = ""
    auth_type: str = "none"


class AddSuggestedOut(BaseModel):
    source_id: str
    detail: str


def _load_cell_ctx(db, cell_id: int, engagement_id: str) -> dict:
    row = db.execute(
        text(
            "SELECT c.cell_id, c.status, c.confidence, c.tam_revenue_usd_m, "
            "       sc.name AS subcat, g.country, c.year "
            "FROM cells c "
            "JOIN taxonomy_subcategories sc ON sc.subcategory_id = c.subcategory_id "
            "JOIN geographies g ON g.geography_id = c.geography_id "
            "WHERE c.cell_id = :cid AND c.engagement_id = :e"
        ),
        {"cid": cell_id, "e": engagement_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Cell not found in this engagement.")
    return dict(row)


def _existing_sources(db, cell_id: int, engagement_id: str) -> list[str]:
    rows = db.execute(
        text(
            "SELECT DISTINCT s.publisher FROM cell_triangulation ct "
            "JOIN sources s ON s.source_id = ct.source_id "
            "WHERE ct.cell_id = :cid AND ct.engagement_id = :e"
        ),
        {"cid": cell_id, "e": engagement_id},
    ).all()
    return [r[0] for r in rows]


@router.post("/{cell_id}/suggest-sources", response_model=SuggestSourcesOut,
             summary="AI suggests concrete sources that would size this cell")
def suggest_sources(
    cell_id: int,
    db: DbSession,
    engagement_id: EngagementDep,
    _user: CurrentUserDep,
) -> SuggestSourcesOut:
    ctx = _load_cell_ctx(db, cell_id, engagement_id)
    existing = _existing_sources(db, cell_id, engagement_id)

    sized = ctx["tam_revenue_usd_m"] is not None
    if not sized:
        diagnosis = ("No source has sized this cell yet — web search found no published "
                     "figure for this specific segment. Add an authoritative source below.")
    elif (ctx["confidence"] or "").lower() == "low":
        diagnosis = ("Sized only from a LOW-confidence web-search estimate. Add a Primary "
                     "(Class A) or Secondary (Class B) source to raise confidence.")
    else:
        diagnosis = "This cell has adequate coverage; more sources would tighten the estimate."

    suggestions: list[SuggestedSource] = []
    if settings.ANTHROPIC_API_KEY:
        try:
            suggestions = _llm_suggest(ctx, existing)
        except Exception as exc:  # noqa: BLE001
            logger.warning("suggest-sources LLM failed: %s", exc)

    if not suggestions:
        suggestions = _fallback_suggest(ctx)

    return SuggestSourcesOut(
        cell_id=cell_id, subcategory=ctx["subcat"], country=ctx["country"],
        year=ctx["year"], diagnosis=diagnosis, existing_sources=existing,
        suggestions=suggestions,
    )


def _llm_suggest(ctx: dict, existing: list[str]) -> list[SuggestedSource]:
    prompt = (
        "You are a market-research sourcing expert. For the specific market cell below, "
        "suggest 3-6 CONCRETE data sources that would provide an authoritative market-size "
        "number, favouring primary (Class A: customs/trade, official statistics, company "
        "filings, regulators) then secondary (Class B: industry analysts) then tertiary "
        "(Class C: news/proxies).\n\n"
        f"Market cell: '{ctx['subcat']}' in {ctx['country']}, year {ctx['year']}.\n"
        f"Sources already used (do NOT repeat these): {json.dumps(existing)}\n\n"
        "Return ONLY a JSON array, no prose. Each item:\n"
        '{"publisher":"<specific org/dataset>","source_class":"<A|B|C>",'
        '"raw_table":"<one of: ' + ", ".join(_RAW_TABLES) + '>",'
        '"base_url":"<https://... best-guess API/portal or \\"\\">",'
        '"endpoint_path":"<path or \\"\\">","auth_type":"<none|api_key|subscription>",'
        '"why":"<one sentence: what number it yields and why it fits this cell>"}'
    )
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": settings.ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": _MODEL, "max_tokens": 1500,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60.0,
    )
    resp.raise_for_status()
    raw = "".join(b.get("text", "") for b in resp.json().get("content", [])
                  if b.get("type") == "text").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    data = json.loads(raw)
    out: list[SuggestedSource] = []
    for s in data if isinstance(data, list) else []:
        rt = s.get("raw_table", "raw_news")
        out.append(SuggestedSource(
            publisher=s.get("publisher", "Suggested source"),
            source_class=s.get("source_class", "B"),
            raw_table=rt if rt in _RAW_TABLES else "raw_news",
            base_url=s.get("base_url", "") or "",
            endpoint_path=s.get("endpoint_path", "") or "",
            auth_type=s.get("auth_type", "none") or "none",
            why=s.get("why", ""),
        ))
    return out


def _fallback_suggest(ctx: dict) -> list[SuggestedSource]:
    """Deterministic archetypes when the LLM is unavailable."""
    return [
        SuggestedSource(publisher="UN Comtrade (HS trade flows)", source_class="A",
                        raw_table="raw_trade_flows", base_url="https://comtradeapi.un.org",
                        endpoint_path="/data/v1/get", auth_type="api_key",
                        why=f"Import/export values for the HS codes mapping to {ctx['subcat']} in {ctx['country']}."),
        SuggestedSource(publisher="Listed-company filings (SEC EDGAR / local exchange)",
                        source_class="A", raw_table="raw_filings",
                        base_url="https://data.sec.gov", endpoint_path="/", auth_type="none",
                        why="Segment revenue of listed players in this category, bottom-up."),
        SuggestedSource(publisher="Industry analyst tracker (IDC / Gartner / Mordor)",
                        source_class="B", raw_table="raw_industry_reports", auth_type="subscription",
                        why="Published TAM/CAGR for this segment as a class-B cross-check."),
    ]


class ResizeCellOut(BaseModel):
    cell_id: int
    tam_revenue_usd_m: float | None
    confidence: str | None
    sources_pulled: list[str]
    messages: list[str]           # per-source outcomes / guidance (keys, auth, endpoints)
    detail: str


# Raw tables whose landed data can be turned into a cell estimate (mirrors
# connectors._RESIZE_METHOD).
_RESIZE_TABLES = [
    "raw_trade_flows", "raw_industry_reports", "raw_regulatory",
    "raw_filings", "raw_external_metrics",
]
_MAX_CELL_SOURCES = 4


@router.post("/{cell_id}/resize", response_model=ResizeCellOut,
             summary="Refresh this cell: re-pull its mapped connectors and re-size it")
def resize_cell(
    cell_id: int,
    db: DbSession,
    engagement_id: EngagementDep,
    _user: CurrentUserDep,
) -> ResizeCellOut:
    """Re-pull the enabled connectors that can feed THIS cell and re-size it in
    place — so after adding a source + key you can refresh the cell without
    leaving it. Reuses the same pull + re-size path as the connector "Pull data
    now" / engagement "Refresh data"; runs synchronously (bounded per source)."""
    ctx = _load_cell_ctx(db, cell_id, engagement_id)
    # Lazy imports to avoid a circular import at module load.
    from connectors.registry import discover, get_connector
    from backend.app.routers.connectors import _resize_after_pull, _RAW_TABLE_COLUMNS
    from backend.app.services import refresh_job
    discover()

    srcs = [dict(r) for r in db.execute(
        text(
            "SELECT source_id, publisher, url_pattern, auth, auth_secret_ref, class, "
            "       connector, raw_table, access_method, notes "
            "FROM sources WHERE engagement_id = :e AND enabled = TRUE "
            "  AND connector IS NOT NULL AND connector <> 'web_search' "
            "  AND raw_table = ANY(:tbls) "
            "ORDER BY class LIMIT :lim"
        ),
        {"e": engagement_id, "tbls": _RESIZE_TABLES, "lim": _MAX_CELL_SOURCES},
    ).mappings().all()]

    pulled: list[str] = []
    messages: list[str] = []
    needs_key = False
    for s in srcs:
        cols = set(_RAW_TABLE_COLUMNS.get(s.get("raw_table"), []))
        r = refresh_job._refresh_source(engagement_id, s, get_connector,
                                        _resize_after_pull, cols)
        reason = r.get("reason") or ""
        if reason:
            messages.append(reason)
        if "needs an API key" in reason or "check the API key" in reason:
            needs_key = True
        if r.get("resized"):
            pulled.append(s["publisher"])

    row = db.execute(
        text("SELECT tam_revenue_usd_m, confidence FROM cells "
             "WHERE cell_id = :c AND engagement_id = :e"),
        {"c": cell_id, "e": engagement_id},
    ).mappings().first()
    tam = float(row["tam_revenue_usd_m"]) if row and row["tam_revenue_usd_m"] is not None else None
    conf = row["confidence"] if row else None

    if pulled:
        detail = (f"Re-sized this cell from {len(pulled)} live source(s): {', '.join(pulled)}. "
                  f"Confidence is now {conf}.")
    elif not srcs:
        detail = ("No live connector feeds this cell yet. Use 'Suggest data sources' below → "
                  "add one → enter its API key on the Connectors page → then Refresh this cell.")
    elif needs_key:
        detail = ("A connector is configured but needs an API key. Open the Connectors page, "
                  "enter the key for the source(s) below, then Refresh this cell again.")
    else:
        detail = ("Connectors ran but returned no usable data for this cell — see the per-source "
                  "notes below (unreachable endpoint, or no rows matched this segment).")
    return ResizeCellOut(cell_id=cell_id, tam_revenue_usd_m=tam, confidence=conf,
                         sources_pulled=pulled, messages=messages, detail=detail)


@router.post("/{cell_id}/add-suggested-source", response_model=AddSuggestedOut,
             summary="Add a suggested source to this engagement (then add a key + pull)")
def add_suggested_source(
    cell_id: int,
    body: AddSuggestedIn,
    db: DbSession,
    engagement_id: EngagementDep,
    _user: CurrentUserDep,
) -> AddSuggestedOut:
    _load_cell_ctx(db, cell_id, engagement_id)  # validates cell ∈ engagement

    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "_", body.publisher.lower()).strip("_")[:40] or "source"
    sid = f"{engagement_id}__{slug}"
    base_url = (body.base_url or "").strip()
    notes = (json.dumps({"endpoints": [{"name": "data", "path": body.endpoint_path or "/"}],
                         "field_map": [], "user_notes": "Added from cell source suggestion."})
             if base_url else "Added from cell source suggestion.")
    db.execute(
        text(
            "INSERT INTO sources (source_id, publisher, url_pattern, auth, class, connector, "
            "  raw_table, access_method, enabled, engagement_id, notes) "
            "VALUES (:sid, :pub, :url, :auth, :cls, :conn, :raw, 'api', :enabled, :eng, :notes) "
            "ON CONFLICT (source_id) DO UPDATE SET url_pattern = EXCLUDED.url_pattern, "
            "  auth = EXCLUDED.auth, notes = EXCLUDED.notes"
        ),
        {
            "sid": sid, "pub": body.publisher, "url": base_url or None,
            "auth": (body.auth_type or "none"), "cls": body.source_class or "B",
            "conn": "generic_rest" if base_url else None,
            "raw": body.raw_table if body.raw_table in _RAW_TABLES else "raw_news",
            "enabled": bool(base_url), "eng": engagement_id, "notes": notes,
        },
    )
    db.commit()
    return AddSuggestedOut(
        source_id=sid,
        detail=(f"Added '{body.publisher}'. Open Connectors → this source → enter its API key, "
                "then 'Pull data now' to size the cell from real data."),
    )
