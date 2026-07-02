"""Brief interpreter API — ``/brief/*``.

Converts a natural-language research brief into a full execution blueprint:
families, geographies, year range, constraints, recommended sources, PLUS
the build-out plan — which connectors will be engaged (and how each payload
is parsed into the raw layer), which estimation methods will triangulate the
cells, and the phased next steps from scope confirmation to maintained asset.

Briefs outside the current engagement's taxonomy (non-medtech verticals) are
not force-mapped: the interpreter flags ``taxonomy_status.in_catalog=false``
and proposes a new family taxonomy for the engagement instead.

If ``ANTHROPIC_API_KEY`` is set the interpreter calls ``claude-sonnet-4-6``
(via the Anthropic Messages API) to parse the brief. If the key is absent
or the call fails, a deterministic rule-based fallback runs instead.
The endpoint **never** fails — it always returns a plausible plan.

DB reads (read-only, never mutates data):
  ``taxonomy_families``  — available family names
  ``geographies``        — available country names (distinct)
  ``sources``            — source catalog (class, raw_table, auth, probe state)
  ``method_registry``    — estimation methods (tier, required raw tables)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.deps import DbSession, CurrentUserDep

logger = logging.getLogger("grx10.routers.brief")

router = APIRouter(prefix="/brief", tags=["brief"])


# ─── I/O shapes ───────────────────────────────────────────────────────────────

class BriefRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Natural-language research brief")


class RecommendedSource(BaseModel):
    source_id: str
    publisher: str
    source_class: str
    why: str


class ConnectorPlanItem(BaseModel):
    source_id: str
    publisher: str
    source_class: str                # A | B | C
    raw_table: str
    access: str                      # e.g. "REST API — key required"
    status: str                      # e.g. "connected" / "catalog — needs credential"
    pulls: str                       # what this connector will pull for THIS brief
    parsing: str                     # how payloads are parsed into the raw layer


class MethodPlanItem(BaseModel):
    method_code: str
    tier: str                        # A | B | C
    description: str
    feeds_from: list[str]            # raw tables consumed
    methodology: str                 # parsing/estimation methodology, brief-scoped


class ExecutionStep(BaseModel):
    step: int
    phase: str                       # e.g. "Scope", "Connect", "Ingest"...
    title: str
    detail: str
    timeline: str                    # e.g. "Day 0-1"


class TaxonomyStatus(BaseModel):
    in_catalog: bool
    proposed_families: list[str] = Field(default_factory=list)
    note: str = ""


class BriefInterpretation(BaseModel):
    families: list[str]
    geographies: list[str]
    years: dict[str, int]            # {"from": <int>, "to": <int>}
    constraints: list[str]
    recommended_sources: list[RecommendedSource]
    interpretation_notes: str
    # Execution blueprint (how the platform will actually build the model)
    taxonomy_status: TaxonomyStatus | None = None
    connector_plan: list[ConnectorPlanItem] = Field(default_factory=list)
    method_plan: list[MethodPlanItem] = Field(default_factory=list)
    execution_plan: list[ExecutionStep] = Field(default_factory=list)


# ─── DB helpers (read-only) ────────────────────────────────────────────────────

def _load_families(db: Session) -> list[str]:
    rows = db.execute(
        text("SELECT name FROM taxonomy_families ORDER BY family_id")
    ).fetchall()
    return [r[0] for r in rows]


def _load_geographies(db: Session) -> list[str]:
    rows = db.execute(
        text("SELECT DISTINCT country FROM geographies ORDER BY country")
    ).fetchall()
    return [r[0] for r in rows]


def _load_sources(db: Session) -> list[dict[str, Any]]:
    """Full source catalog with the metadata the connector plan needs."""
    rows = db.execute(
        text(
            "SELECT source_id, publisher, class, notes, raw_table, auth, "
            "       access_method, enabled, last_probe_status, refresh_cadence "
            "FROM sources "
            "ORDER BY class, source_id"
        )
    ).fetchall()
    return [
        {
            "source_id": r[0],
            "publisher": r[1],
            "class": r[2] or "B",
            "notes": r[3] or "",
            "raw_table": r[4] or "",
            "auth": (r[5] or "none").lower(),
            "access_method": r[6] or "api",
            "enabled": bool(r[7]),
            "last_probe_status": r[8] or "",
            "refresh_cadence": r[9] or "",
        }
        for r in rows
    ]


def _load_methods(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            "SELECT method_code, description, tier, source_class, "
            "       confidence_cap, required_raw_tables "
            "FROM method_registry ORDER BY tier, method_code"
        )
    ).fetchall()
    return [
        {
            "method_code": r[0],
            "description": r[1] or "",
            "tier": r[2] or "C",
            "source_class": r[3] or "",
            "confidence_cap": r[4] or "",
            "required_raw_tables": list(r[5] or []),
        }
        for r in rows
    ]


# ─── Parsing methodology per raw layer (grounding for both interpreters) ─────

PARSING_BY_RAW_TABLE: dict[str, str] = {
    "raw_trade_flows": (
        "REST JSON → typed trade rows (reporter, partner, HS code, flow, period, "
        "value USD). An HS→subcategory crosswalk allocates flows to taxonomy cells; "
        "import values are grossed up to apparent market via domestic-production shares."
    ),
    "raw_filings": (
        "Filings/XBRL → segment-disclosure extraction (filer, segment, region, "
        "revenue, FX-normalised to USD). Segment revenue ÷ the filer's estimated "
        "share ⇒ bottom-up market size per cell."
    ),
    "raw_regulatory": (
        "Registry queries → registration records (holder, product class code, "
        "status). Active-registration counts × per-registration unit economics ⇒ "
        "market size; holder mix also feeds player shares."
    ),
    "raw_industry_reports": (
        "Report/table extraction → published TAM, CAGR and segment splits per "
        "country. Allocated top-down to cells; kept as a class-B cross-check "
        "against primary methods."
    ),
    "raw_external_metrics": (
        "REST JSON indicators (health/industry expenditure, macro, activity "
        "volumes) → typed indicator facts. Feed activity-volume × ASP models and "
        "macro-scaling of known cells to peer geographies."
    ),
    "raw_procurement": (
        "OCDS/tender JSON → award records (buyer, item, value, date). Award "
        "aggregation by category ⇒ public-demand market floor."
    ),
    "raw_news": (
        "RSS/GDELT streams → dated article snippets. Event detection flags "
        "catalysts (M&A, capacity, regulation); web-search extraction writes "
        "LOW-capped estimates with the snippet stored verbatim."
    ),
    "raw_transcripts": (
        "Interview & earnings-call text → attributed claims (speaker, date, "
        "quote). Share/revenue claims parsed into triangulation evidence; "
        "relationship claims feed the supplier graph."
    ),
    "raw_patents": (
        "Patent APIs → filing records by assignee and class. Filing activity is "
        "an innovation/output proxy (class C)."
    ),
    "raw_signals": (
        "Job boards/statistical APIs → hiring and capacity signals per company "
        "and site. Capacity-expansion proxy (class C)."
    ),
    "raw_shipments": (
        "Bill-of-lading records → shipment lines (shipper, consignee, HS, "
        "weight). Aggregated to trade-lane volumes."
    ),
    "raw_standards": (
        "Standards-body registries → membership/participation records; used as "
        "a production-footprint proxy."
    ),
}


def _access_label(src: dict[str, Any]) -> str:
    method = {
        "api": "REST API", "scrape": "Scrape (flagged)", "manual": "Document extraction",
        "manual_upload": "Document upload", "interview": "Interview program",
        "rss": "RSS feed",
    }.get(src["access_method"].lower(), src["access_method"].upper() or "API")
    if src["auth"] in ("", "none", "no", "public"):
        return f"{method} — public"
    if "key" in src["auth"] or "token" in src["auth"] or src["auth"] == "api_key":
        return f"{method} — key required"
    if "subscription" in src["auth"] or "license" in src["auth"]:
        return f"{method} — subscription"
    return f"{method} — {src['auth']}"


def _status_label(src: dict[str, Any]) -> str:
    if src["enabled"]:
        if src["last_probe_status"] == "OK":
            return "connected — probe OK"
        return "connected"
    if src["auth"] not in ("", "none", "no", "public"):
        return "catalog — needs credential"
    return "catalog — one-click enable"


# ─── Execution-blueprint builders (shared by both interpreters) ──────────────

# Geography-affine sources: picked when that country is in scope.
_GEO_SOURCES = {
    "China": ["cn_nmpa", "cn_nhsa_vbp", "mindray_filings", "microport_filings",
              "united_imaging_hkex"],
    "Malaysia": ["malaysia_mda", "malaysia_matrade", "mida"],
    "Singapore": ["sg_hsa", "sg_enterprise", "sg_edb"],
}
# Backbone sources: engaged for almost any brief (trade, reports, metrics).
_BACKBONE_SOURCES = [
    "un_comtrade", "imarc", "globaldata", "who_gho", "world_bank",
    "primary_exec_interview",
]


def _pulls_text(raw_table: str, families: list[str], geographies: list[str],
                years: dict[str, int]) -> str:
    fam = ", ".join(families[:3]) + ("…" if len(families) > 3 else "") or "all in-scope families"
    geo = ", ".join(geographies[:4]) or "in-scope geographies"
    span = f"{years.get('from', 2026)}–{years.get('to', 2031)}"
    return {
        "raw_trade_flows": f"Import/export flows for the HS codes mapped to {fam}; reporters: {geo}; latest 3 actual years feeding {span} estimates.",
        "raw_filings": f"Segment & regional revenue disclosures of listed players active in {fam} across {geo}.",
        "raw_regulatory": f"Active product registrations per category for {fam} in {geo} (holder, class code, status).",
        "raw_industry_reports": f"Published TAM/CAGR tables covering {fam} in {geo}, {span}.",
        "raw_external_metrics": f"Health/industry expenditure, activity volumes and macro indicators for {geo} (scaling inputs).",
        "raw_procurement": f"Public tender awards matching {fam} categories in {geo}.",
        "raw_news": f"News/RSS events for {fam} players in {geo} — catalysts + LOW-capped web-search extraction fallback.",
        "raw_transcripts": f"Expert interviews & earnings calls covering {fam} in {geo} — share and relationship claims.",
    }.get(raw_table, f"Signals relevant to {fam} in {geo}.")


def _build_connector_plan(
    sources: list[dict[str, Any]],
    families: list[str],
    geographies: list[str],
    years: dict[str, int],
) -> list[ConnectorPlanItem]:
    """Deterministic connector selection: backbone + geo-affine + coverage fill.

    Aims for breadth across the raw layer (so tier-A methods can triangulate)
    while preferring class-A, already-enabled sources.
    """
    by_id = {s["source_id"]: s for s in sources}
    picked_ids: list[str] = [s for s in _BACKBONE_SOURCES if s in by_id]
    for geo in geographies:
        picked_ids += [s for s in _GEO_SOURCES.get(geo, []) if s in by_id]

    # Sources tied to a geography that is NOT in scope must never be picked
    # (a China-only registry has nothing to say about a China-excluded brief).
    out_of_scope = {
        sid for geo, sids in _GEO_SOURCES.items()
        if geo not in geographies for sid in sids
    } - set(picked_ids)

    # Ensure at least one source per high-value raw table (coverage fill).
    # raw_procurement is deliberately absent: every procurement source is
    # country-bound, so it only enters via geography affinity above.
    covered = {by_id[s]["raw_table"] for s in picked_ids}
    for tbl in ("raw_trade_flows", "raw_filings", "raw_regulatory",
                "raw_industry_reports", "raw_external_metrics",
                "raw_transcripts", "raw_news"):
        if tbl in covered:
            continue
        candidates = sorted(
            (s for s in sources
             if s["raw_table"] == tbl and s["source_id"] not in out_of_scope),
            key=lambda s: (not s["enabled"], s["class"]),
        )
        if candidates:
            picked_ids.append(candidates[0]["source_id"])
            covered.add(tbl)

    seen: set[str] = set()
    plan: list[ConnectorPlanItem] = []
    for sid in picked_ids:
        if sid in seen:
            continue
        seen.add(sid)
        src = by_id[sid]
        plan.append(ConnectorPlanItem(
            source_id=sid,
            publisher=src["publisher"],
            source_class=src["class"],
            raw_table=src["raw_table"],
            access=_access_label(src),
            status=_status_label(src),
            pulls=_pulls_text(src["raw_table"], families, geographies, years),
            parsing=PARSING_BY_RAW_TABLE.get(src["raw_table"], "Typed normalization into the raw layer."),
        ))
    return plan


def _build_method_plan(
    methods: list[dict[str, Any]],
    connector_plan: list[ConnectorPlanItem],
) -> list[MethodPlanItem]:
    """Methods whose required raw tables are covered by the connector plan."""
    covered = {c.raw_table for c in connector_plan}
    plan: list[MethodPlanItem] = []
    for m in methods:
        required = m["required_raw_tables"]
        if required and not set(required) & covered:
            continue
        cap = f" Confidence hard-capped at {m['confidence_cap'].upper()}." if m["confidence_cap"] else ""
        plan.append(MethodPlanItem(
            method_code=m["method_code"],
            tier=m["tier"],
            description=m["description"],
            feeds_from=required,
            methodology=(
                (m["description"].rstrip(".") + ". " if m["description"] else "")
                + PARSING_BY_RAW_TABLE.get(required[0] if required else "", "")
                + cap
            ).strip(),
        ))
    return plan


def _build_execution_plan(
    families: list[str],
    geographies: list[str],
    years: dict[str, int],
    connector_plan: list[ConnectorPlanItem],
    method_plan: list[MethodPlanItem],
    taxonomy: TaxonomyStatus,
) -> list[ExecutionStep]:
    n_fam = len(families) or len(taxonomy.proposed_families) or 7
    n_geo = len(geographies) or 3
    n_years = max(1, years.get("to", 2031) - years.get("from", 2026) + 1)
    approx_cells = n_fam * 4 * n_geo * min(n_years, 2)
    needs_credential = [c.source_id for c in connector_plan if "credential" in c.status]
    tier_a = sum(1 for m in method_plan if m.tier == "A")

    scope_title = (
        "Confirm scope & seed taxonomy"
        if taxonomy.in_catalog
        else "Draft & approve the new engagement taxonomy"
    )
    scope_detail = (
        f"Lock the plan you see here: {n_fam} families × {n_geo} geographies × "
        f"{years.get('from')}–{years.get('to')}. Subcategories, HS-code and "
        "regulatory-code crosswalks are versioned into the taxonomy spine."
        if taxonomy.in_catalog
        else (
            "This brief is outside the current engagement's taxonomy. Draft "
            f"{max(4, len(taxonomy.proposed_families))} families with subcategories, "
            "HS-code and regulatory-code crosswalks; approve before ingestion."
        )
    )
    cred_detail = (
        f"Enable the {len(connector_plan)} planned connectors. "
        + (f"Credentials needed for: {', '.join(needs_credential[:5])}. "
           if needs_credential else "")
        + "Each connector is probed on save (7-state health check) and budget "
          "ceilings are set before the first pull."
    )
    return [
        ExecutionStep(step=1, phase="Scope", title=scope_title,
                      detail=scope_detail, timeline="Day 0–1"),
        ExecutionStep(step=2, phase="Connect", title="Register connectors & credentials",
                      detail=cred_detail, timeline="Day 1–2"),
        ExecutionStep(step=3, phase="Ingest", title="Scheduled ingestion into the raw layer",
                      detail=(f"Pull the planned scope across {len(connector_plan)} sources into "
                              "the 12-table raw layer. Every payload lands verbatim (JSONB) with "
                              "typed columns for drill-down; failures surface on the Status page."),
                      timeline="Day 2–5"),
        ExecutionStep(step=4, phase="Parse", title="Normalize & parse to typed evidence",
                      detail=("Connector-specific parsers map payloads to typed rows: HS→subcategory "
                              "crosswalks, XBRL segment extraction, registration-record parsing, "
                              "report-table extraction. Every row keeps its source_id — the audit "
                              "chain is built here."),
                      timeline="Day 2–5"),
        ExecutionStep(step=5, phase="Estimate", title=f"Triangulate ~{approx_cells} cells",
                      detail=(f"{len(method_plan)} estimation methods ({tier_a} tier-A) run against "
                              "the raw layer; each cell needs ≥2 independent methods before it "
                              "enters the model. Player shares & supplier graph build alongside."),
                      timeline="Week 1–2"),
        ExecutionStep(step=6, phase="Score", title="Confidence scoring & gap review",
                      detail=("The confidence engine scores every cell (COUNT(DISTINCT method), "
                              "spread ratio, independence at method × source-class) under the "
                              "Standard validation profile. LOW/gap cells are triaged: add a "
                              "connector, or accept a LOW-capped web-search estimate."),
                      timeline="Week 2"),
        ExecutionStep(step=7, phase="Deliver", title="Deliverables & scheduled refresh",
                      detail=("Dashboards, drillable Cell Explorer, PDF reports and hyperlinked "
                              "Excel exports go live. The pipeline stays on schedule so the model "
                              "is a maintained asset, not a one-off report."),
                      timeline="Week 2–3, then ongoing"),
    ]


# ─── Rule-based fallback ──────────────────────────────────────────────────────

# Each tuple: (keyword_fragments, canonical_family_name).
# Keyword fragments are matched case-insensitively against the brief.
_FAMILY_RULES: list[tuple[list[str], str]] = [
    (
        ["cardiovascular", "cardiac", "vascular", "heart", "angioplasty", "coronary", "stent"],
        "Cardiovascular & Vascular",
    ),
    (
        ["imaging", "radiology", "mri", "ct scan", "ultrasound", "x-ray", "xray", "radiograph", "pacs"],
        "Medical Imaging",
    ),
    (
        ["diagnostic", "ivd", "in vitro", "laboratory", "lab test", "assay", "reagent", "immunoassay", "pcr"],
        "In Vitro Diagnostics",
    ),
    (
        ["surgical", "endoscop", "gastrointestinal", "laparoscop", "colonoscop", "gi device"],
        "Surgical & GI Endoscopy",
    ),
    (
        ["monitoring", "critical care", "icu", "patient monitor", "ventilator", "anesthes", "infusion pump"],
        "Patient Monitoring & Critical Care",
    ),
    (
        ["consumable", "disposable", "glove", "syringe", "wound care", "bandage", "dressing"],
        "Consumables",
    ),
    (
        ["orthopedic", "orthopaed", "spine", "spinal", "implant", "joint replace", "bone", "hip replace", "knee replace", "trauma fixation"],
        "Orthopedics & Spine",
    ),
]

_SE_ASIA_TERMS = ["se asia", "southeast asia", "south-east asia", "apac", "asia pacific"]


def _rule_based_interpret(
    input_text: str,
    all_families: list[str],
    all_geographies: list[str],
    all_sources: list[dict[str, Any]],
    all_methods: list[dict[str, Any]],
) -> BriefInterpretation:
    lower = input_text.lower()

    # ── Families + taxonomy status ────────────────────────────────────────────
    matched_families: list[str] = []
    for keywords, family_name in _FAMILY_RULES:
        if any(kw in lower for kw in keywords):
            if family_name in all_families and family_name not in matched_families:
                matched_families.append(family_name)

    domain_hints = ("medtech", "medical", "med-tech", "device", "health", "hospital", "clinical")
    if matched_families:
        taxonomy = TaxonomyStatus(
            in_catalog=True,
            note="Brief maps onto the engagement's existing taxonomy.",
        )
    elif any(h in lower for h in domain_hints):
        matched_families = list(all_families)  # generic medtech brief: all families
        taxonomy = TaxonomyStatus(
            in_catalog=True,
            note="No specific segment named — all catalog families in scope by default.",
        )
    else:
        # Non-medtech vertical: do NOT force-map to the medtech taxonomy.
        taxonomy = TaxonomyStatus(
            in_catalog=False,
            proposed_families=[],
            note=(
                "This brief appears to target a vertical outside the current "
                "engagement's taxonomy. A new family/subcategory taxonomy will be "
                "drafted from the brief (with HS-code and regulatory crosswalks) "
                "as step 1 of the execution plan. Enable the AI interpreter "
                "(ANTHROPIC_API_KEY) for an automatic taxonomy proposal."
            ),
        )

    # ── Geographies ───────────────────────────────────────────────────────────
    geo_set: set[str] = set()
    if any(t in lower for t in _SE_ASIA_TERMS):
        geo_set = set(all_geographies)
    else:
        for geo in all_geographies:
            if geo.lower() in lower:
                geo_set.add(geo)

    # Apply "exclude X" removals
    for m in re.finditer(r"exclude\s+([a-z ]+?)(?:[,;.\n]|$)", lower):
        excluded = m.group(1).strip()
        geo_set = {g for g in geo_set if excluded not in g.lower()}

    if not geo_set:
        geo_set = set(all_geographies)  # default: all

    geographies = sorted(geo_set)

    # ── Year range ────────────────────────────────────────────────────────────
    year_from, year_to = 2026, 2031
    m = re.search(r"(20\d{2})\s*[-–to]+\s*(20\d{2})", lower)
    if m:
        year_from = int(m.group(1))
        year_to = int(m.group(2))
    else:
        single = re.search(r"\b(20\d{2})\b", lower)
        if single:
            y = int(single.group(1))
            year_from = min(y, 2026)
            year_to = max(y, 2031)

    # ── Constraints ───────────────────────────────────────────────────────────
    constraints: list[str] = []
    seen_constraints: set[str] = set()
    for m in re.finditer(r"exclude\s+([^,;.\n]+)", input_text, re.IGNORECASE):
        c = f"Exclude {m.group(1).strip()}"
        if c not in seen_constraints:
            constraints.append(c)
            seen_constraints.add(c)
    for m in re.finditer(
        r"(?:focus(?:ed)? on|limited to|only)\s+([^,;.\n]+)",
        input_text,
        re.IGNORECASE,
    ):
        c = f"Focus: {m.group(1).strip()}"
        if c not in seen_constraints:
            constraints.append(c)
            seen_constraints.add(c)

    # ── Recommended sources ───────────────────────────────────────────────────
    recommended = _pick_sources(
        [s for s in all_sources if s["enabled"] and s["class"] in ("A", "B")]
    )

    # ── Execution blueprint ───────────────────────────────────────────────────
    years = {"from": year_from, "to": year_to}
    connector_plan = _build_connector_plan(
        all_sources, matched_families, geographies, years
    )
    method_plan = _build_method_plan(all_methods, connector_plan)
    execution_plan = _build_execution_plan(
        matched_families, geographies, years, connector_plan, method_plan, taxonomy
    )

    n_fam = len(matched_families)
    n_geo = len(geographies)
    notes = (
        f"Rule-based interpretation (Anthropic API not available). "
        f"Matched {n_fam} {'family' if n_fam == 1 else 'families'}, "
        f"{n_geo} {'geography' if n_geo == 1 else 'geographies'}, "
        f"years {year_from}–{year_to}. Connector plan: {len(connector_plan)} "
        f"sources feeding {len(method_plan)} estimation methods."
    )

    return BriefInterpretation(
        families=matched_families,
        geographies=geographies,
        years=years,
        constraints=constraints,
        recommended_sources=recommended,
        interpretation_notes=notes,
        taxonomy_status=taxonomy,
        connector_plan=connector_plan,
        method_plan=method_plan,
        execution_plan=execution_plan,
    )


def _pick_sources(all_sources: list[dict[str, Any]]) -> list[RecommendedSource]:
    """Return up to 8 class-A/B sources with human-readable rationale."""
    seen: set[str] = set()
    picked: list[RecommendedSource] = []

    # Class-A first (authoritative primary sources)
    for src in all_sources:
        if src["class"] != "A" or src["source_id"] in seen:
            continue
        note_snippet = src["notes"][:120].rstrip(".") + "." if src["notes"] else ""
        picked.append(
            RecommendedSource(
                source_id=src["source_id"],
                publisher=src["publisher"],
                source_class="A",
                why=(
                    f"Class-A authoritative source. {note_snippet} "
                    "Provides primary triangulation data for trade flows and regulatory registrations."
                ).strip(),
            )
        )
        seen.add(src["source_id"])
        if len(picked) >= 5:
            break

    # Class-B fill up to 8
    for src in all_sources:
        if src["class"] != "B" or src["source_id"] in seen or len(picked) >= 8:
            continue
        note_snippet = src["notes"][:120].rstrip(".") + "." if src["notes"] else ""
        picked.append(
            RecommendedSource(
                source_id=src["source_id"],
                publisher=src["publisher"],
                source_class="B",
                why=(
                    f"Class-B secondary source. {note_snippet} "
                    "Useful for cross-validation and regional demand signals."
                ).strip(),
            )
        )
        seen.add(src["source_id"])

    return picked


# ─── Anthropic interpreter ────────────────────────────────────────────────────

def _call_anthropic(
    input_text: str,
    all_families: list[str],
    all_geographies: list[str],
    all_sources: list[dict[str, Any]],
    all_methods: list[dict[str, Any]],
    api_key: str,
) -> BriefInterpretation:
    families_json = json.dumps(all_families)
    geos_json = json.dumps(all_geographies)
    sources_json = json.dumps(
        [
            {
                "source_id": s["source_id"],
                "publisher": s["publisher"],
                "class": s["class"],
                "raw_table": s["raw_table"],
                "access": _access_label(s),
                "status": _status_label(s),
            }
            for s in all_sources
        ]
    )
    methods_json = json.dumps(
        [
            {
                "method_code": m["method_code"],
                "tier": m["tier"],
                "description": m["description"],
                "required_raw_tables": m["required_raw_tables"],
                "confidence_cap": m["confidence_cap"],
            }
            for m in all_methods
        ]
    )
    parsing_json = json.dumps(PARSING_BY_RAW_TABLE)

    prompt = (
        "You are the research-planning engine of GRX10's automated market-research "
        "platform. The platform turns a brief into a maintained, source-traceable "
        "market model: connectors ingest raw payloads, parsers normalize them into "
        "a typed raw layer, estimation methods triangulate every market cell "
        "(subcategory x geography x year), and a confidence engine scores each cell.\n\n"
        f"Current engagement taxonomy families: {families_json}\n"
        f"Geographies already modelled: {geos_json}\n"
        f"Source catalog (use EXACT source_id values): {sources_json}\n"
        f"Estimation methods (use EXACT method_code values): {methods_json}\n"
        f"Default parsing methodology per raw table: {parsing_json}\n\n"
        'Given the brief below, produce the full research plan. '
        'Return ONLY a single JSON object, no markdown fences, no explanation:\n\n'
        f'Brief: """{input_text}"""\n\n'
        "Required JSON schema:\n"
        "{\n"
        '  "families": ["<exact family name from list, ONLY if the brief fits the current taxonomy>"],\n'
        '  "geographies": ["<country name; prefer the modelled list, but new countries are allowed for new engagements>"],\n'
        '  "years": {"from": <int>, "to": <int>},\n'
        '  "constraints": ["<free-text constraint phrase>"],\n'
        '  "taxonomy_status": {\n'
        '    "in_catalog": <true if the brief\'s product domain fits the current taxonomy>,\n'
        '    "proposed_families": ["<4-8 proposed family names — ONLY when in_catalog is false>"],\n'
        '    "note": "<1-2 sentences on the taxonomy decision>"\n'
        "  },\n"
        '  "recommended_sources": [\n'
        '    {"source_id": "<id>", "publisher": "<name>", "source_class": "<A|B|C>", "why": "<one sentence>"}\n'
        "  ],\n"
        '  "connector_plan": [\n'
        "    {\n"
        '      "source_id": "<id from catalog>", "publisher": "<name>", "source_class": "<A|B|C>",\n'
        '      "raw_table": "<raw table>", "access": "<from catalog>", "status": "<from catalog>",\n'
        '      "pulls": "<SPECIFIC to this brief: what will be pulled — name real HS-code ranges, registries, filers, indicators>",\n'
        '      "parsing": "<how payloads are parsed/normalized — adapt the default parsing methodology to this brief>"\n'
        "    }\n"
        "  ],\n"
        '  "method_plan": [\n'
        '    {"method_code": "<code>", "tier": "<A|B|C>", "description": "<from registry>",\n'
        '     "feeds_from": ["<raw tables>"], "methodology": "<2-3 sentences: how this method computes the estimate for THIS brief>"}\n'
        "  ],\n"
        '  "execution_plan": [\n'
        '    {"step": <1..7>, "phase": "<Scope|Connect|Ingest|Parse|Estimate|Score|Deliver>",\n'
        '     "title": "<short>", "detail": "<2-3 sentences, concrete and scoped to this brief>", "timeline": "<e.g. Day 0-1>"}\n'
        "  ],\n"
        '  "interpretation_notes": "<1-2 sentences explaining ambiguous choices>"\n'
        "}\n\n"
        "Rules:\n"
        "- families: only names appearing EXACTLY in the provided list. If the brief targets a "
        "different vertical (not covered by the taxonomy), leave families empty, set "
        "taxonomy_status.in_catalog=false and propose a sensible new family taxonomy instead — "
        "NEVER force-map an unrelated vertical onto the current taxonomy.\n"
        "- 'SE Asia', 'Southeast Asia', 'APAC' means include ALL modelled geographies. "
        "'exclude X' removes X. New engagements may add geographies not yet modelled.\n"
        "- Default year range when unspecified: {\"from\": 2026, \"to\": 2031}.\n"
        "- connector_plan: 8-14 sources. Prefer class-A and already-connected sources; always "
        "cover trade flows, filings, regulatory, industry reports and external metrics so "
        "tier-A methods can triangulate. Include the interview program and one news source.\n"
        "- method_plan: every method whose required_raw_tables are covered by the connector "
        "plan; mention the LOW cap where it applies.\n"
        "- execution_plan: exactly 7 steps (Scope, Connect, Ingest, Parse, Estimate, Score, "
        "Deliver) with realistic timelines; reference actual counts (connectors, methods, "
        "approximate cells).\n"
        "- constraints: capture 'exclude X' and 'focus on X' clauses verbatim."
    )

    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90.0,
    )
    resp.raise_for_status()

    raw_text: str = resp.json()["content"][0]["text"].strip()
    # Strip markdown code fences if the model wraps in them despite instructions
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    parsed = json.loads(raw_text)

    rec_sources = [
        RecommendedSource(
            source_id=s["source_id"],
            publisher=s.get("publisher", s["source_id"]),
            source_class=s.get("source_class", "B"),
            why=s.get("why", ""),
        )
        for s in parsed.get("recommended_sources", [])
    ]

    families = parsed.get("families", [])
    geographies = parsed.get("geographies", all_geographies)
    years = parsed.get("years", {"from": 2026, "to": 2031})

    tax_raw = parsed.get("taxonomy_status") or {}
    taxonomy = TaxonomyStatus(
        in_catalog=bool(tax_raw.get("in_catalog", bool(families))),
        proposed_families=list(tax_raw.get("proposed_families", [])),
        note=tax_raw.get("note", ""),
    )

    connector_plan = [
        ConnectorPlanItem(
            source_id=c.get("source_id", ""),
            publisher=c.get("publisher", c.get("source_id", "")),
            source_class=c.get("source_class", "B"),
            raw_table=c.get("raw_table", ""),
            access=c.get("access", "REST API"),
            status=c.get("status", "catalog"),
            pulls=c.get("pulls", ""),
            parsing=c.get("parsing", PARSING_BY_RAW_TABLE.get(c.get("raw_table", ""), "")),
        )
        for c in parsed.get("connector_plan", [])
    ]
    method_plan = [
        MethodPlanItem(
            method_code=m.get("method_code", ""),
            tier=m.get("tier", "C"),
            description=m.get("description", ""),
            feeds_from=list(m.get("feeds_from", [])),
            methodology=m.get("methodology", ""),
        )
        for m in parsed.get("method_plan", [])
    ]
    execution_plan = [
        ExecutionStep(
            step=int(s.get("step", i + 1)),
            phase=s.get("phase", ""),
            title=s.get("title", ""),
            detail=s.get("detail", ""),
            timeline=s.get("timeline", ""),
        )
        for i, s in enumerate(parsed.get("execution_plan", []))
    ]

    # Guarantee the blueprint sections are never empty: fall back to the
    # deterministic builders when the model omits or truncates them.
    if not connector_plan:
        connector_plan = _build_connector_plan(all_sources, families, geographies, years)
    if not method_plan:
        method_plan = _build_method_plan(all_methods, connector_plan)
    if not execution_plan:
        execution_plan = _build_execution_plan(
            families, geographies, years, connector_plan, method_plan, taxonomy
        )

    return BriefInterpretation(
        families=families,
        geographies=geographies,
        years=years,
        constraints=parsed.get("constraints", []),
        recommended_sources=rec_sources,
        interpretation_notes=parsed.get(
            "interpretation_notes", "Interpreted by Claude."
        ),
        taxonomy_status=taxonomy,
        connector_plan=connector_plan,
        method_plan=method_plan,
        execution_plan=execution_plan,
    )


# ─── Endpoint ─────────────────────────────────────────────────────────────────

@router.post(
    "/interpret",
    response_model=BriefInterpretation,
    summary="Interpret a natural-language research brief into a structured plan",
)
def interpret_brief(
    body: BriefRequest,
    db: DbSession,
    _user: CurrentUserDep,
) -> BriefInterpretation:
    """Parse a free-text research brief into families, geographies, year range,
    constraints, and recommended sources.

    Uses the Anthropic Messages API (``claude-sonnet-4-6``) when
    ``ANTHROPIC_API_KEY`` is configured; falls back to a deterministic
    rule-based interpreter otherwise. Never fails — always returns a
    plausible structured plan. Reads real data from the DB (taxonomy_families,
    geographies, sources); never fabricates entities.
    """
    all_families = _load_families(db)
    all_geographies = _load_geographies(db)
    all_sources = _load_sources(db)
    all_methods = _load_methods(db)

    api_key = settings.ANTHROPIC_API_KEY
    if api_key:
        try:
            return _call_anthropic(
                body.text, all_families, all_geographies, all_sources,
                all_methods, api_key,
            )
        except Exception as exc:  # noqa: BLE001 — AI is best-effort; always fall back
            logger.warning(
                "Anthropic brief interpretation failed; using rule-based fallback: %s",
                exc,
            )

    return _rule_based_interpret(
        body.text, all_families, all_geographies, all_sources, all_methods
    )
