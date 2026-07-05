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
    base_url: str = ""               # e.g. "https://api.anac.gov.br"
    endpoint_path: str = ""          # e.g. "/v1/drones"
    auth_type: str = ""              # "none" | "api_key" | "subscription" | ""


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


class ProposedSubcategory(BaseModel):
    family: str            # the family name this subcategory belongs to
    name: str
    hs_codes: list[str] = Field(default_factory=list)
    regulatory_codes: list[str] = Field(default_factory=list)


class BriefInterpretation(BaseModel):
    families: list[str]
    geographies: list[str]
    years: dict[str, int]            # {"from": <int>, "to": <int>}
    constraints: list[str]
    recommended_sources: list[RecommendedSource]
    interpretation_notes: str
    # Execution blueprint (how the platform will actually build the model)
    taxonomy_status: TaxonomyStatus | None = None
    proposed_subcategories: list[ProposedSubcategory] = Field(default_factory=list)
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

def _loads_salvage(raw: str) -> dict[str, Any]:
    """Parse model JSON, tolerating truncation.

    A large new-vertical plan can be cut off at the token limit, leaving an
    unterminated string / unclosed brackets. Rather than lose the entire plan to
    the rule-based fallback, we (1) try a clean parse, then (2) trim to the last
    balanced position and close any open brackets so the salvageable prefix
    (families, taxonomy, most connectors) still yields a usable object.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Trim a dangling unterminated string, then walk to the last position where
    # brackets balance and close the rest.
    depth_stack: list[str] = []
    in_str = False
    esc = False
    last_ok = -1
    for i, ch in enumerate(raw):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                depth_stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if depth_stack:
                    depth_stack.pop()
            elif ch == "," and not depth_stack:
                break
        if not in_str:
            last_ok = i
    candidate = raw[: last_ok + 1]
    # Drop a trailing comma, then close any still-open brackets (deepest last).
    candidate = candidate.rstrip().rstrip(",")
    # Recompute open brackets for the trimmed candidate.
    stack2: list[str] = []
    in_str = esc = False
    for ch in candidate:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack2.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack2:
            stack2.pop()
    if in_str:
        candidate += '"'
    candidate += "".join(reversed(stack2))
    return json.loads(candidate)




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
        [{"method_code": m["method_code"], "tier": m["tier"]} for m in all_methods]
    )

    # COMPACT contract: the LLM makes the *judgment* calls (taxonomy fit, which
    # sources/methods, new-vertical proposals) and we expand the verbose
    # connector/method/execution prose deterministically from the builders. This
    # keeps the generation small (~hundreds of tokens, a few seconds) instead of
    # asking for the entire blueprint verbatim (~8k tokens, >90s → timeout).
    prompt = (
        "You are the research-planning engine of GRX10's automated market-research "
        "platform (connectors → typed raw layer → triangulation methods → "
        "confidence-scored cells).\n\n"
        f"Current taxonomy families: {families_json}\n"
        f"Geographies already modelled: {geos_json}\n"
        f"Source catalog (EXACT source_id values): {sources_json}\n"
        f"Estimation methods (EXACT method_code values): {methods_json}\n\n"
        'Given the brief, return ONLY one JSON object (no markdown, no prose):\n'
        f'Brief: """{input_text}"""\n\n'
        "{\n"
        '  "families": ["<exact catalog family; [] if the brief is a different vertical>"],\n'
        '  "geographies": ["<country; modelled names preferred, new ones allowed>"],\n'
        '  "years": {"from": <int>, "to": <int>},\n'
        '  "constraints": ["<verbatim exclude/focus clauses>"],\n'
        '  "taxonomy_status": {"in_catalog": <bool>, "proposed_families": ["<4-8 names ONLY if in_catalog=false>"], "note": "<1-2 sentences>"},\n'
        '  "proposed_subcategories": [{"family":"<one of proposed_families>","name":"<subcategory>","hs_codes":["<6-digit HS>"],"regulatory_codes":["<code>"]}],\n'
        '  "source_ids": ["<EXACT catalog source_id to engage, when in_catalog>"],\n'
        '  "proposed_connectors": [{"publisher":"<name>","source_class":"<A|B|C>","raw_table":"<one of the raw_* tables>","access":"<e.g. REST API — key required>","base_url":"<https://api.host.gov>","path":"<e.g. /v1/resource>","auth":"<none|api_key|subscription>","pulls":"<one line: what it pulls for this brief>"}],\n'
        '  "method_codes": ["<EXACT method_code to run>"],\n'
        '  "interpretation_notes": "<1-2 sentences on ambiguous choices>"\n'
        "}\n\n"
        "Rules:\n"
        "- families: EXACT catalog names only. If the brief targets a vertical NOT in the "
        "taxonomy, set families=[], taxonomy_status.in_catalog=false, propose 4-8 new families, "
        "and populate proposed_connectors (8-12) with plausible authorities for that vertical "
        "(trade/customs, regulators, filings, industry reports, macro metrics, news). Each "
        "proposed_connector MUST include base_url, path and auth (none|api_key|subscription) so a "
        "generic-REST connector can attempt a pull. NEVER force-map an unrelated vertical onto the "
        "medtech taxonomy.\n"
        "- When in_catalog=false, also fill proposed_subcategories: for EACH proposed family give "
        "2-4 subcategories, each with best-guess hs_codes (6-digit ok) and any regulatory_codes. "
        "When in_catalog=true leave proposed_subcategories=[] (subcategories already exist in the "
        "catalog).\n"
        "- When in_catalog=true, fill source_ids (8-14 EXACT ids) covering trade flows, filings, "
        "regulatory, industry reports and external metrics so tier-A methods can triangulate; "
        "leave proposed_connectors and proposed_subcategories empty.\n"
        "- source_ids: prefer class-A, already-connected sources. Do NOT include a country-bound "
        "source for a geography not in scope.\n"
        "- method_codes: every method whose raw tables your sources cover.\n"
        "- 'SE Asia'/'APAC' = all modelled geographies; 'exclude X' removes X. "
        "Default years {\"from\":2026,\"to\":2031}."
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
            # Headroom for the new-vertical case (proposed families + 2-4
            # subcategories each with HS/regulatory codes + 8-12 proposed
            # connectors with endpoints). This output is large; without enough
            # room it truncates → unterminated JSON → the whole plan is lost to
            # the rule-based fallback (which dumps irrelevant catalog sources).
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=150.0,
    )
    resp.raise_for_status()

    raw_text: str = resp.json()["content"][0]["text"].strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    parsed = _loads_salvage(raw_text)

    families = parsed.get("families", [])
    geographies = parsed.get("geographies") or all_geographies
    years = parsed.get("years", {"from": 2026, "to": 2031})

    tax_raw = parsed.get("taxonomy_status") or {}
    taxonomy = TaxonomyStatus(
        in_catalog=bool(tax_raw.get("in_catalog", bool(families))),
        proposed_families=list(tax_raw.get("proposed_families", [])),
        note=tax_raw.get("note", ""),
    )

    # ── Expand the compact decision into the verbose blueprint ────────────────
    by_id = {s["source_id"]: s for s in all_sources}
    connector_plan: list[ConnectorPlanItem] = []

    # In-catalog: map the LLM's chosen source_ids onto real catalog connectors,
    # generating pulls/parsing prose deterministically.
    for sid in parsed.get("source_ids", []):
        src = by_id.get(sid)
        if not src:
            continue
        connector_plan.append(ConnectorPlanItem(
            source_id=sid,
            publisher=src["publisher"],
            source_class=src["class"],
            raw_table=src["raw_table"],
            access=_access_label(src),
            status=_status_label(src),
            pulls=_pulls_text(src["raw_table"], families, geographies, years),
            parsing=PARSING_BY_RAW_TABLE.get(src["raw_table"], "Typed normalization into the raw layer."),
        ))

    # New vertical: the LLM proposes connectors that don't exist in the catalog yet.
    # Each carries a candidate REST endpoint (base_url + path + auth) so a generic
    # REST connector can attempt a pull once the engagement is materialized.
    for i, c in enumerate(parsed.get("proposed_connectors", [])):
        raw_table = c.get("raw_table", "")
        connector_plan.append(ConnectorPlanItem(
            source_id=f"proposed_{i+1}",
            publisher=c.get("publisher", f"Proposed source {i+1}"),
            source_class=c.get("source_class", "B"),
            raw_table=raw_table,
            access=c.get("access", "REST API"),
            status="proposed — to onboard",
            pulls=c.get("pulls", _pulls_text(raw_table, taxonomy.proposed_families or families, geographies, years)),
            parsing=PARSING_BY_RAW_TABLE.get(raw_table, "Typed normalization into the raw layer."),
            base_url=c.get("base_url", "") or "",
            endpoint_path=c.get("path", "") or "",
            auth_type=c.get("auth", "") or "",
        ))

    # New vertical: proposed subcategories (with HS / regulatory codes) that will
    # seed the materialized engagement's cells. Parsed defensively (missing → empty).
    proposed_subcategories: list[ProposedSubcategory] = []
    for s in parsed.get("proposed_subcategories", []) or []:
        if not isinstance(s, dict):
            continue
        name = s.get("name", "")
        if not name:
            continue
        proposed_subcategories.append(ProposedSubcategory(
            family=s.get("family", "") or "",
            name=name,
            hs_codes=list(s.get("hs_codes", []) or []),
            regulatory_codes=list(s.get("regulatory_codes", []) or []),
        ))

    if not connector_plan:
        connector_plan = _build_connector_plan(all_sources, families, geographies, years)

    # Methods: honour the LLM's picks when valid, else derive from raw-table coverage.
    method_by_code = {m["method_code"]: m for m in all_methods}
    covered_tables = {c.raw_table for c in connector_plan}
    chosen = [method_by_code[mc] for mc in parsed.get("method_codes", []) if mc in method_by_code]
    method_source = chosen or all_methods
    method_plan = _build_method_plan(
        [m for m in method_source
         if not m["required_raw_tables"] or set(m["required_raw_tables"]) & covered_tables],
        connector_plan,
    )
    if not method_plan:
        method_plan = _build_method_plan(all_methods, connector_plan)

    execution_plan = _build_execution_plan(
        families, geographies, years, connector_plan, method_plan, taxonomy
    )

    # Recommended-sources list (legacy panel) derived from the connector plan.
    rec_sources = [
        RecommendedSource(
            source_id=c.source_id, publisher=c.publisher,
            source_class=c.source_class,
            why=c.pulls[:140],
        )
        for c in connector_plan[:8]
    ]

    return BriefInterpretation(
        families=families,
        geographies=geographies,
        years=years,
        constraints=parsed.get("constraints", []),
        recommended_sources=rec_sources,
        interpretation_notes=parsed.get("interpretation_notes", "Interpreted by Claude."),
        taxonomy_status=taxonomy,
        proposed_subcategories=proposed_subcategories,
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
