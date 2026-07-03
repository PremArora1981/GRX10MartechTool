"""Pydantic v2 response/request schemas.

These are the API's wire contracts. The centrepiece is :class:`CellDetail`, which
carries the **full drill chain** mandated by the spec — every number is
two-click-drillable from cell -> estimate -> source -> raw payload reference —
so the frontend Cell Detail screen can render the audit trail without extra
round-trips.

All models use ``from_attributes=True`` so they can be built directly from
SQLAlchemy ORM instances (``CellSummary.model_validate(cell_obj)``).
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]
SourceClass = Literal["A", "B", "C"]


class _ORMModel(BaseModel):
    """Base for schemas hydrated from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Health / meta
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    """Payload for ``GET /health`` (drives the Render health check + status page)."""

    status: Literal["ok", "degraded"]
    service: str = "grx10-mr-api"
    version: str
    database: Literal["up", "down"]
    auth_configured: bool
    time: datetime.datetime


# --------------------------------------------------------------------------- #
# Spine reference schemas
# --------------------------------------------------------------------------- #
class GeographyOut(_ORMModel):
    geography_id: int
    country: str
    segment: str  # DOMESTIC | IMPORT | EXPORT | SELF_CONSUME ...


class SubcategoryOut(_ORMModel):
    subcategory_id: int
    family_id: int
    name: str
    hs_codes: list[str] = Field(default_factory=list)
    regulatory_codes: list[str] = Field(default_factory=list)
    version: int = 1


class SourceOut(_ORMModel):
    """Public view of a source. Never carries secret material (only the pointer)."""

    source_id: str
    publisher: str
    url_pattern: str | None = None
    auth: str | None = None
    source_class: SourceClass | None = Field(default=None, alias="source_class")
    connector: str | None = None
    refresh_cadence: str | None = None
    raw_table: str | None = None
    access_method: str | None = None
    enabled: bool | None = None
    last_probe_status: str | None = None
    last_probe_at: datetime.datetime | None = None
    last_probe_detail: str | None = None
    monthly_budget: Decimal | None = None
    quota_ceiling: int | None = None


class MethodOut(_ORMModel):
    method_code: str
    description: str | None = None
    tier: SourceClass | None = None
    source_class: str | None = None
    is_primary_source: bool | None = None
    confidence_cap: Confidence | None = None
    required_raw_tables: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Drill chain: raw reference -> estimate -> cell
# --------------------------------------------------------------------------- #
class RawRef(BaseModel):
    """A drillable pointer to the verbatim payload backing an estimate.

    The spec requires two-click drill to the *raw payload*. The estimate's
    triangulation row records the ``source_id``; the matching raw rows live in
    that source's ``raw_table``. This carries enough to fetch them
    (``GET /raw/{raw_table}?source_id=...`` or by ``raw_id``) plus a small inline
    sample so the UI can show evidence immediately.
    """

    source_id: str
    raw_table: str | None = Field(
        default=None, description="The raw_* table that holds this source's payloads."
    )
    raw_id: int | None = Field(
        default=None, description="Specific raw row id, when a method pinned one."
    )
    accessed_at: datetime.datetime | None = None
    sample: dict[str, Any] | None = Field(
        default=None, description="Inline verbatim JSON sample for immediate display."
    )


class EstimateOut(_ORMModel):
    """One ``cell_triangulation`` row: a method's estimate from one source.

    Carries its source (and, when resolvable, a raw reference) so the Cell Detail
    estimates table is itself the audit chain — one row per method, each drillable
    to its source and raw payload.
    """

    triangulation_id: int
    cell_id: int
    method_code: str
    estimate_usd_m: Decimal
    source_id: str
    notes: str | None = None
    computed_at: datetime.datetime | None = None

    # Enriched (joined) context — populated by the cell-detail service, optional
    # so a bare ORM CellTriangulation still validates.
    method: MethodOut | None = None
    source: SourceOut | None = None
    raw_ref: RawRef | None = None


class TriangulationSummaryOut(_ORMModel):
    """Read-only projection of ``cell_triangulation_summary`` (confidence math)."""

    cell_id: int
    n_estimates: int | None = None
    n_distinct_methods: int | None = None
    n_independent_signals: int | None = None
    n_source_classes: int | None = None
    estimate_min: Decimal | None = None
    estimate_median: Decimal | None = None
    estimate_max: Decimal | None = None
    spread_ratio: Decimal | None = None
    has_tier_a: bool | None = None
    effective_signals: int | None = None
    qualifies_high: bool | None = None
    qualifies_medium: bool | None = None


# --------------------------------------------------------------------------- #
# Cells
# --------------------------------------------------------------------------- #
class CellSummary(_ORMModel):
    """Row shape for the Cell Explorer list view (sub-second, paginated).

    Always carries the TAM band and confidence chip (acceptance criteria).
    """

    cell_id: int
    subcategory_id: int
    geography_id: int
    year: int
    tam_revenue_usd_m: Decimal | None = None
    tam_low_usd_m: Decimal | None = None
    tam_high_usd_m: Decimal | None = None
    tam_units: int | None = None
    confidence: Confidence | None = None
    confidence_rationale: str | None = None
    status: str | None = None
    updated_at: datetime.datetime | None = None

    # Denormalised labels for display (optional; filled by the list service join).
    subcategory_name: str | None = None
    country: str | None = None
    segment: str | None = None


class CellDetail(CellSummary):
    """Full Cell Detail payload — the complete drill chain for one cell.

    ``estimates`` is one row per method (each -> source -> raw ref); ``summary``
    exposes the confidence math from the view. Together they let the UI render the
    audit trail (TAM -> band -> per-method estimates -> source URL/publisher/
    accessed_at -> raw payload) with no further fetches.
    """

    subcategory: SubcategoryOut | None = None
    geography: GeographyOut | None = None
    estimates: list[EstimateOut] = Field(default_factory=list)
    summary: TriangulationSummaryOut | None = None


class CellList(BaseModel):
    """Paginated Cell Explorer response."""

    items: list[CellSummary]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- #
# Players
# --------------------------------------------------------------------------- #
class CompanyOut(_ORMModel):
    company_id: int
    name: str
    company_type: str | None = None
    country_hq: str | None = None
    seeded_role: str | None = None
    discovered: bool | None = None


class PlayerShareOut(_ORMModel):
    share_id: int
    cell_id: int
    company_id: int
    player_role: str
    rank: int
    share_pct: Decimal | None = None
    share_low_pct: Decimal | None = None
    share_high_pct: Decimal | None = None
    revenue_usd_m: Decimal | None = None
    source_id: str
    confidence: str | None = None
    # Joined company context — populated by the players router (joinedload).
    company: CompanyOut | None = None


class SupplierRelationshipOut(_ORMModel):
    """Buyer-supplier edge from ``supplier_relationships``, drillable via source_id."""

    relationship_id: int
    buyer_id: int
    supplier_id: int
    cell_id: int | None = None
    relationship_type: str
    evidence_type: str
    evidence_strength: str
    source_id: str
    notes: str | None = None
    # Joined company context — populated by the players router (joinedload).
    buyer: CompanyOut | None = None
    supplier: CompanyOut | None = None


class PlayerShareList(BaseModel):
    """Paginated ``player_shares`` for a single cell."""

    items: list[PlayerShareOut]
    total: int
    limit: int
    offset: int
    cell_id: int


class SupplierRelationshipList(BaseModel):
    """Paginated ``supplier_relationships`` for a single cell."""

    items: list[SupplierRelationshipOut]
    total: int
    limit: int
    offset: int
    cell_id: int


# --------------------------------------------------------------------------- #
# Assumptions / commentary
# --------------------------------------------------------------------------- #
class AssumptionCreate(BaseModel):
    """Request body for ``POST /assumptions``.

    All scope fields are optional; a fully-null scope creates a global
    (engagement-wide) assumption.  When a prior assumption with the same scope
    exists and has no ``superseded_by``, the API wires the prior's
    ``superseded_by`` to point at the new row — the prior is never overwritten.
    """

    scope_company_id: int | None = None
    scope_subcategory_id: int | None = None
    scope_geography_id: int | None = None
    assumption_text: str
    numeric_value: Decimal | None = None
    unit: str | None = None
    confidence: str | None = None
    derivation_method: str | None = None
    source_id: str | None = None
    effective_from_year: int
    effective_to_year: int | None = None


class AssumptionOut(_ORMModel):
    assumption_id: int
    scope_company_id: int | None = None
    scope_subcategory_id: int | None = None
    scope_geography_id: int | None = None
    assumption_text: str
    numeric_value: Decimal | None = None
    unit: str | None = None
    confidence: str | None = None
    derivation_method: str | None = None
    source_id: str | None = None
    effective_from_year: int
    effective_to_year: int | None = None
    superseded_by: int | None = None
    created_at: datetime.datetime | None = None


class AssumptionList(BaseModel):
    """Paginated assumptions ledger."""

    items: list[AssumptionOut]
    total: int
    limit: int
    offset: int


class AssumptionInfluencedCells(BaseModel):
    """Reverse drill: cells linked to an assumption via ``cell_assumption_link``."""

    assumption_id: int
    items: list[CellSummary]
    total: int
    limit: int
    offset: int


class CommentaryOut(_ORMModel):
    commentary_id: int
    scope_type: str
    scope_id: int | None = None
    body_markdown: str
    audience: str | None = None
    author: str | None = None
    created_at: datetime.datetime | None = None


# --------------------------------------------------------------------------- #
# Engagements (multi-engagement workspace)
# --------------------------------------------------------------------------- #
class EngagementOut(_ORMModel):
    """Public view of a row from the ``engagements`` table."""

    engagement_id: str
    name: str
    is_demo: bool
    status: str  # active | archived
    active_profile: str
    web_search_enabled: bool
    brief_text: str | None = None
    created_at: datetime.datetime | None = None


class EngagementCreate(BaseModel):
    """Request body for creating an engagement from a confirmed brief plan.

    ``plan`` is intentionally permissive so it can carry the full confirmed
    connector/method/subcategory plan without a rigid schema.
    """

    name: str
    brief_text: str | None = None
    families: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)
    year_from: int
    year_to: int
    plan: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Auth (current user) — shape shared with the auth service (deps.py)
# --------------------------------------------------------------------------- #
Role = Literal["owner", "admin", "analyst", "business", "external"]


class CurrentUser(BaseModel):
    """Authenticated principal resolved from the WorkOS AuthKit session (Q10)."""

    id: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    role: Role = "external"
    organization_id: str | None = None

    @property
    def is_admin(self) -> bool:
        """``owner``/``admin`` gate credential entry (Q9) + audience switcher."""
        return self.role in ("owner", "admin")


__all__ = [
    "Confidence", "SourceClass", "Role",
    "HealthResponse",
    "GeographyOut", "SubcategoryOut", "SourceOut", "MethodOut",
    "RawRef", "EstimateOut", "TriangulationSummaryOut",
    "CellSummary", "CellDetail", "CellList",
    # players
    "CompanyOut", "PlayerShareOut", "SupplierRelationshipOut",
    "PlayerShareList", "SupplierRelationshipList",
    # assumptions
    "AssumptionCreate", "AssumptionOut", "AssumptionList", "AssumptionInfluencedCells",
    # commentary
    "CommentaryOut",
    # engagements
    "EngagementOut", "EngagementCreate",
    # auth
    "CurrentUser",
]
