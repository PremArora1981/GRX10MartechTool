"""Stats/aggregates router — feeds the dashboard landing (W1).

Routes
------
GET /stats/overview?year=2026
    Aggregated headline figures from the cells + taxonomy + geographies tables:
    total TAM, cell count, confidence breakdown (count + TAM per band),
    TAM by product family, TAM by geography (country-level across all segments).

    All numeric values are plain Python floats — never Decimal strings — so the
    JSON payload is unambiguously numeric and safe to pass directly into Recharts.

Auto-discovered by main.py's router scanner (module exposes ``router: APIRouter``).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, nullslast, select

from backend.app.deps import DbSession, EngagementDep
from backend.app.models import Cell, Geography, TaxonomyFamily, TaxonomySubcategory

logger = logging.getLogger("grx10.routers.stats")

router = APIRouter(prefix="/stats", tags=["stats"])


# ---------------------------------------------------------------------------
# Response schemas (self-contained — not in schemas.py which is shared)
# ---------------------------------------------------------------------------


class ConfidenceSplit(BaseModel):
    """Count of cells + sum of TAM for one confidence band."""

    count: int
    tam_usd_m: float


class ConfidenceBreakdown(BaseModel):
    high: ConfidenceSplit
    medium: ConfidenceSplit
    low: ConfidenceSplit


class FamilyRow(BaseModel):
    family: str
    tam_usd_m: float
    share: float  # 0–100 percent


class GeoRow(BaseModel):
    country: str
    tam_usd_m: float
    share: float  # 0–100 percent


class OverviewResponse(BaseModel):
    year: int
    total_tam_usd_m: float
    cell_count: int
    confidence_breakdown: ConfidenceBreakdown
    by_family: list[FamilyRow]
    by_geography: list[GeoRow]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/overview", response_model=OverviewResponse)
def get_overview(
    db: DbSession,
    engagement_id: EngagementDep,
    year: Annotated[
        int | None,
        Query(description="Model year; defaults to the engagement's latest year with cells."),
    ] = None,
) -> OverviewResponse:
    """Aggregated headline figures for the dashboard landing.

    Joins ``cells → taxonomy_subcategories → taxonomy_families`` for the family
    breakdown and ``cells → geographies`` for the geography breakdown. Postgres
    NUMERIC columns arrive as ``Decimal`` objects; they are coerced to ``float``
    before serialisation so the JSON payload carries plain numbers, not strings
    (GOTCHA #1 from project docs).

    When ``year`` is omitted it resolves to the engagement's most recent modelled
    year (so a new engagement whose anchor years aren't 2026 still shows data).
    """
    if year is None:
        # The near anchor year (MIN) is the base year the dashboard opens on:
        # preserves Medtech's 2026 headline and opens a new engagement on its
        # first modelled year rather than the far-forecast one.
        year = db.execute(
            select(func.min(Cell.year)).where(Cell.engagement_id == engagement_id)
        ).scalar_one_or_none() or 2026
    # -- Per-confidence counts + TAM (one pass) --------------------------------
    conf_stmt = (
        select(
            Cell.confidence,
            func.count(Cell.cell_id).label("cnt"),
            func.coalesce(func.sum(Cell.tam_revenue_usd_m), 0).label("tam"),
        )
        .where(Cell.year == year, Cell.engagement_id == engagement_id)
        .group_by(Cell.confidence)
    )
    conf_rows = db.execute(conf_stmt).all()

    conf_map: dict[str, tuple[int, float]] = {}
    total_count = 0
    total_tam = 0.0
    for row in conf_rows:
        band = (row.confidence or "unknown").lower()
        cnt = int(row.cnt)
        tam = float(row.tam)
        conf_map[band] = (cnt, tam)
        total_count += cnt
        total_tam += tam

    def _conf_split(band: str) -> ConfidenceSplit:
        cnt, tam = conf_map.get(band, (0, 0.0))
        return ConfidenceSplit(count=cnt, tam_usd_m=round(tam, 2))

    # -- By family (cells → subcategories → families) -------------------------
    family_stmt = (
        select(
            TaxonomyFamily.name.label("family"),
            func.coalesce(func.sum(Cell.tam_revenue_usd_m), 0).label("tam"),
        )
        .join(
            TaxonomySubcategory,
            Cell.subcategory_id == TaxonomySubcategory.subcategory_id,
        )
        .join(
            TaxonomyFamily,
            TaxonomySubcategory.family_id == TaxonomyFamily.family_id,
        )
        .where(Cell.year == year, Cell.engagement_id == engagement_id)
        .group_by(TaxonomyFamily.name)
        .order_by(nullslast(func.sum(Cell.tam_revenue_usd_m).desc()))
    )
    family_rows = db.execute(family_stmt).all()
    by_family = [
        FamilyRow(
            family=str(r.family),
            tam_usd_m=round(float(r.tam), 2),
            share=round(float(r.tam) / total_tam * 100, 2) if total_tam else 0.0,
        )
        for r in family_rows
    ]

    # -- By geography (country-level aggregate across all segments) -----------
    geo_stmt = (
        select(
            Geography.country,
            func.coalesce(func.sum(Cell.tam_revenue_usd_m), 0).label("tam"),
        )
        .join(Geography, Cell.geography_id == Geography.geography_id)
        .where(Cell.year == year, Cell.engagement_id == engagement_id)
        .group_by(Geography.country)
        .order_by(nullslast(func.sum(Cell.tam_revenue_usd_m).desc()))
    )
    geo_rows = db.execute(geo_stmt).all()
    by_geography = [
        GeoRow(
            country=str(r.country),
            tam_usd_m=round(float(r.tam), 2),
            share=round(float(r.tam) / total_tam * 100, 2) if total_tam else 0.0,
        )
        for r in geo_rows
    ]

    return OverviewResponse(
        year=year,
        total_tam_usd_m=round(total_tam, 2),
        cell_count=total_count,
        confidence_breakdown=ConfidenceBreakdown(
            high=_conf_split("high"),
            medium=_conf_split("medium"),
            low=_conf_split("low"),
        ),
        by_family=by_family,
        by_geography=by_geography,
    )
