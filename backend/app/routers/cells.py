"""Cells router — read-only, three endpoints.

Routes
------
GET /api/cells
    Paginated Cell Explorer: filter by subcategory_id, geography_id, year,
    and/or confidence band. Sub-second thanks to the composite index
    ``idx_cells_sub_geo_year`` + ``idx_cells_confidence``. Every item carries its
    TAM band (low/revenue/high) and confidence chip (acceptance criterion).

GET /api/cells/{cell_id}
    Full drill chain for one cell:
      cell -> estimates[] -> (method + tier + source) -> (url/publisher/accessed_at)
              -> raw row ref (raw_table, raw_id, inline sample)
    No further fetches needed by the frontend to render the complete audit trail.

GET /api/cells/{cell_id}/triangulation-summary
    Confidence math projection from the ``cell_triangulation_summary`` materialised
    view: COUNT(DISTINCT method_code) counts, spread ratio, and ``qualifies_high`` /
    ``qualifies_medium`` booleans computed against the ACTIVE validation profile.

All endpoints are read-only. Auth is enforced; all four roles may read.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, nullslast, select
from sqlalchemy.orm import Session, joinedload

from backend.app.deps import CurrentUserDep, DbSession
from backend.app.models import (
    Cell,
    CellTriangulation,
    CellTriangulationSummary,
    RAW_TABLE_MODELS,
    Source,
)
from backend.app.schemas import (
    CellDetail,
    CellList,
    CellSummary,
    EstimateOut,
    GeographyOut,
    MethodOut,
    RawRef,
    SourceOut,
    SubcategoryOut,
    TriangulationSummaryOut,
)

logger = logging.getLogger("grx10.routers.cells")

router = APIRouter(prefix="/cells", tags=["cells"])

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CONFIDENCE_VALUES = frozenset({"high", "medium", "low"})


def _cell_summary(cell: Cell) -> CellSummary:
    """Construct a :class:`CellSummary` from a fully-loaded ``Cell`` ORM row.

    Assumes the ORM object has its ``subcategory`` and ``geography`` relationships
    already eager-loaded (joinedload) to avoid extra round-trips.
    """
    return CellSummary(
        cell_id=cell.cell_id,
        subcategory_id=cell.subcategory_id,
        geography_id=cell.geography_id,
        year=cell.year,
        tam_revenue_usd_m=cell.tam_revenue_usd_m,
        tam_low_usd_m=cell.tam_low_usd_m,
        tam_high_usd_m=cell.tam_high_usd_m,
        tam_units=cell.tam_units,
        confidence=cell.confidence,
        confidence_rationale=cell.confidence_rationale,
        status=cell.status,
        updated_at=cell.updated_at,
        # Denormalised display labels from the eager-loaded relationships.
        subcategory_name=cell.subcategory.name if cell.subcategory else None,
        country=cell.geography.country if cell.geography else None,
        segment=cell.geography.segment if cell.geography else None,
    )


def _raw_match_filters(model, raw_table: str, cell: Cell | None) -> list:
    """Column filters that narrow a raw-row lookup to *cell*'s own evidence.

    Each raw table carries different typed columns; where the connector's
    ``normalize()`` fills them we can match the cell's country / subcategory /
    HS codes so two cells drilling into the same source land on their own raw
    rows. Reference-year columns (``period``) are deliberately NOT matched
    against the cell year: forecast cells (e.g. 2031) legitimately drill to the
    latest *actual* evidence the estimate was derived from.
    """
    if cell is None:
        return []
    country = cell.geography.country if cell.geography else None
    subcat = cell.subcategory.name if cell.subcategory else None
    hs_codes = (cell.subcategory.hs_codes or []) if cell.subcategory else []

    filters: list = []
    if raw_table == "raw_trade_flows":
        if country:
            filters.append(model.reporter == country)
        if hs_codes:
            filters.append(model.hs_code.in_(hs_codes))
    elif raw_table == "raw_industry_reports":
        if subcat and country:
            filters.append(model.market == f"{subcat} - {country}")
            filters.append(model.period == str(cell.year))
    elif raw_table == "raw_external_metrics":
        if country:
            filters.append(model.country == country)
    elif raw_table == "raw_regulatory":
        if country:
            filters.append(model.country == country)
        if subcat:
            filters.append(
                model.raw_json["query"]["product_category"].astext == subcat
            )
    elif raw_table == "raw_filings":
        if country:
            filters.append(model.geography == country)
        if subcat:
            filters.append(model.segment == subcat)
    return filters


def _resolve_raw_ref(db: Session, source: Source, cell: Cell | None = None) -> RawRef:
    """Return a :class:`RawRef` pointing to the raw evidence row for *source*.

    Looks up ``source.raw_table`` in :data:`RAW_TABLE_MODELS` and matches the
    cell's country / subcategory / HS codes (see :func:`_raw_match_filters`) so
    each cell drills to its own evidence; falls back to the most-recent row for
    the source when no cell-scoped row matches. If the table is unknown or empty
    the ref is still returned (with ``raw_id=None``) so the frontend can at
    least display the source URL / publisher.

    Never raises — degraded data (missing raw) is surfaced as a partial ref, not
    an HTTP error.
    """
    raw_table = source.raw_table
    base_ref = dict(source_id=source.source_id, raw_table=raw_table)

    if not raw_table or raw_table not in RAW_TABLE_MODELS:
        return RawRef(**base_ref)

    model = RAW_TABLE_MODELS[raw_table]
    try:
        row = None
        cell_filters = _raw_match_filters(model, raw_table, cell)
        if cell_filters:
            row = (
                db.execute(
                    select(model)
                    .where(model.source_id == source.source_id, *cell_filters)  # type: ignore[attr-defined]
                    .order_by(nullslast(model.accessed_at.desc()))  # type: ignore[attr-defined]
                    .limit(1)
                )
                .scalars()
                .first()
            )
        if row is None:
            row = (
                db.execute(
                    select(model)
                    .where(model.source_id == source.source_id)  # type: ignore[attr-defined]
                    .order_by(nullslast(model.accessed_at.desc()))  # type: ignore[attr-defined]
                    .limit(1)
                )
                .scalars()
                .first()
            )
    except Exception:  # noqa: BLE001 — raw table may not yet have data
        logger.debug(
            "raw_ref lookup skipped for source %s / table %s",
            source.source_id,
            raw_table,
            exc_info=True,
        )
        return RawRef(**base_ref)

    if row is None:
        return RawRef(**base_ref)

    return RawRef(
        source_id=source.source_id,
        raw_table=raw_table,
        raw_id=row.raw_id,
        accessed_at=row.accessed_at,
        # Inline verbatim JSON sample (always a dict in JSONB; guard just in case).
        sample=row.raw_json if isinstance(row.raw_json, dict) else None,
    )


def _build_estimate(
    tri: CellTriangulation, db: Session, cell: Cell | None = None
) -> EstimateOut:
    """Build a fully-enriched :class:`EstimateOut` from a triangulation row.

    Embeds the method's tier + confidence_cap, the source's publisher /
    url_pattern / last_probe_status, and a raw_ref pointer so the Cell Detail
    estimates table is itself the two-click audit trail with no extra fetches.
    Passing *cell* lets the raw_ref resolver match the cell's own evidence row.

    Assumes ``tri.method`` and ``tri.source`` have already been eager-loaded.
    """
    method_out: MethodOut | None = (
        MethodOut.model_validate(tri.method) if tri.method else None
    )
    source_out: SourceOut | None = (
        SourceOut.model_validate(tri.source) if tri.source else None
    )
    raw_ref: RawRef | None = (
        _resolve_raw_ref(db, tri.source, cell) if tri.source else None
    )

    return EstimateOut(
        triangulation_id=tri.triangulation_id,
        cell_id=tri.cell_id,
        method_code=tri.method_code,
        estimate_usd_m=tri.estimate_usd_m,
        source_id=tri.source_id,
        notes=tri.notes,
        computed_at=tri.computed_at,
        method=method_out,
        source=source_out,
        raw_ref=raw_ref,
    )


# ---------------------------------------------------------------------------
# GET /api/cells — paginated Cell Explorer
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=CellList,
    summary="List cells — paginated Cell Explorer",
    description=(
        "Returns a paginated, filterable list of cells. "
        "Every item carries the TAM band (low/revenue/high) and its confidence chip. "
        "Uses the composite DB index (subcategory_id, geography_id, year) for "
        "sub-second response even at scale."
    ),
)
def list_cells(
    db: DbSession,
    _user: CurrentUserDep,
    subcategory_id: Annotated[
        int | None,
        Query(description="Filter by taxonomy subcategory_id"),
    ] = None,
    geography_id: Annotated[
        int | None,
        Query(description="Filter by geography_id"),
    ] = None,
    year: Annotated[
        int | None,
        Query(description="Filter by calendar year"),
    ] = None,
    confidence: Annotated[
        str | None,
        Query(description="Filter by confidence band: high | medium | low"),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=500, description="Number of items per page (max 500)"),
    ] = 50,
    offset: Annotated[
        int,
        Query(ge=0, description="Zero-based page offset"),
    ] = 0,
) -> CellList:
    """Paginated Cell Explorer list.

    Filters are ANDed. Default ordering is ``year DESC``, then ``cell_id`` for
    stable pagination. The composite index ``idx_cells_sub_geo_year`` keeps list
    queries sub-second; the ``idx_cells_confidence`` index covers confidence-only
    filters. Joins to ``taxonomy_subcategories`` and ``geographies`` are done via
    ``joinedload`` in the same round-trip so display labels cost nothing extra.
    """
    if confidence is not None and confidence not in _CONFIDENCE_VALUES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"confidence must be one of: {sorted(_CONFIDENCE_VALUES)}",
        )

    # Build the shared WHERE predicate list.
    filters = []
    if subcategory_id is not None:
        filters.append(Cell.subcategory_id == subcategory_id)
    if geography_id is not None:
        filters.append(Cell.geography_id == geography_id)
    if year is not None:
        filters.append(Cell.year == year)
    if confidence is not None:
        filters.append(Cell.confidence == confidence)

    # --- COUNT (separate scalar query so paginators know total pages) ---
    count_stmt = select(func.count()).select_from(Cell)
    if filters:
        count_stmt = count_stmt.where(*filters)
    total: int = db.execute(count_stmt).scalar_one()

    # --- DATA (joinedload for display labels, no extra queries) ---
    data_stmt = (
        select(Cell)
        .options(
            joinedload(Cell.subcategory),
            joinedload(Cell.geography),
        )
        .order_by(Cell.year.desc(), Cell.cell_id)
        .limit(limit)
        .offset(offset)
    )
    if filters:
        data_stmt = data_stmt.where(*filters)

    cells = db.execute(data_stmt).unique().scalars().all()

    return CellList(
        items=[_cell_summary(c) for c in cells],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /api/cells/{cell_id} — full drill chain
# ---------------------------------------------------------------------------

@router.get(
    "/{cell_id}",
    response_model=CellDetail,
    summary="Cell detail — full drill chain (cell → estimates → source → raw ref)",
)
def get_cell(
    cell_id: int,
    db: DbSession,
    _user: CurrentUserDep,
) -> CellDetail:
    """Return the complete drill chain for one cell.

    ``estimates`` is one row per ``(method_code, source_id)`` pair from
    ``cell_triangulation``. Each estimate embeds:

    * ``method`` — method_code, description, **tier** (A/B/C), confidence_cap
    * ``source`` — publisher, url_pattern, access_method, last_probe_status,
      last_probe_at (supports the "accessed_at" acceptance criterion)
    * ``raw_ref`` — raw_table name, raw_id of the most-recent row, accessed_at,
      and an inline JSON sample for immediate evidence display

    Together this lets the Cell Detail UI render the full audit trail
    (TAM → band → per-method estimate → source URL → raw payload) with a single
    API call. Every number carries its TAM band (``tam_low_usd_m``,
    ``tam_revenue_usd_m``, ``tam_high_usd_m``) and confidence chip.
    """
    stmt = (
        select(Cell)
        .options(
            joinedload(Cell.subcategory),
            joinedload(Cell.geography),
            # Nested eager loads: triangulation -> method AND triangulation -> source
            joinedload(Cell.triangulations).joinedload(CellTriangulation.method),
            joinedload(Cell.triangulations).joinedload(CellTriangulation.source),
        )
        .where(Cell.cell_id == cell_id)
    )
    cell = db.execute(stmt).unique().scalars().first()
    if cell is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cell {cell_id} not found.",
        )

    # Build enriched EstimateOut rows (one per triangulation row).
    # _build_estimate issues one cheap indexed query per unique source to locate
    # the latest raw row; with typically 2-5 estimates per cell this is fast.
    estimates = [_build_estimate(tri, db, cell) for tri in cell.triangulations]

    # Triangulation summary from the materialised view (may be absent if the
    # pipeline refresh hasn't run yet or the cell has no estimates).
    summary_row = db.get(CellTriangulationSummary, cell_id)
    summary: TriangulationSummaryOut | None = (
        TriangulationSummaryOut.model_validate(summary_row) if summary_row else None
    )

    return CellDetail(
        cell_id=cell.cell_id,
        subcategory_id=cell.subcategory_id,
        geography_id=cell.geography_id,
        year=cell.year,
        tam_revenue_usd_m=cell.tam_revenue_usd_m,
        tam_low_usd_m=cell.tam_low_usd_m,
        tam_high_usd_m=cell.tam_high_usd_m,
        tam_units=cell.tam_units,
        confidence=cell.confidence,
        confidence_rationale=cell.confidence_rationale,
        status=cell.status,
        updated_at=cell.updated_at,
        # Display labels (flat, for list-compatible consumers).
        subcategory_name=cell.subcategory.name if cell.subcategory else None,
        country=cell.geography.country if cell.geography else None,
        segment=cell.geography.segment if cell.geography else None,
        # Full nested objects for the drill-chain UI.
        subcategory=(
            SubcategoryOut.model_validate(cell.subcategory) if cell.subcategory else None
        ),
        geography=(
            GeographyOut.model_validate(cell.geography) if cell.geography else None
        ),
        estimates=estimates,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# GET /cells/{cell_id}/triangulation  — per-method estimate list (drill chain)
# ---------------------------------------------------------------------------

@router.get(
    "/{cell_id}/triangulation",
    summary="Per-method triangulation estimates for one cell (method → source → raw)",
)
def get_cell_triangulation(
    cell_id: int,
    db: DbSession,
    _user: CurrentUserDep,
) -> list[dict]:
    """Return one estimate per ``cell_triangulation`` row, FLATTENED.

    The Cell Detail estimates table (``CellTriangulationView``) consumes flat
    fields (``method_tier``, ``source_publisher``, ``source_url``,
    ``source_class``, ``source_access_method``), so we flatten the joined
    method/source here rather than nesting. Same rows as ``GET /cells/{id}``.
    """
    stmt = (
        select(Cell)
        .options(
            joinedload(Cell.triangulations).joinedload(CellTriangulation.method),
            joinedload(Cell.triangulations).joinedload(CellTriangulation.source),
        )
        .where(Cell.cell_id == cell_id)
    )
    cell = db.execute(stmt).unique().scalars().first()
    if cell is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cell {cell_id} not found.",
        )
    out: list[dict] = []
    for tri in cell.triangulations:
        est = _build_estimate(tri, db, cell)  # EstimateOut with nested method/source
        m, s = est.method, est.source
        out.append({
            "triangulation_id": est.triangulation_id,
            "cell_id": est.cell_id,
            "method_code": est.method_code,
            "estimate_usd_m": est.estimate_usd_m,
            "source_id": est.source_id,
            "notes": est.notes,
            "computed_at": est.computed_at,
            "method_description": m.description if m else None,
            "method_tier": m.tier if m else None,
            "source_publisher": s.publisher if s else None,
            "source_url": s.url_pattern if s else None,
            "source_class": s.source_class if s else None,
            "source_access_method": s.access_method if s else None,
        })
    return out


# ---------------------------------------------------------------------------
# GET /cells/{cell_id}/triangulation/{triangulation_id}/raw — verbatim payload
# ---------------------------------------------------------------------------

@router.get(
    "/{cell_id}/triangulation/{triangulation_id}/raw",
    summary="Verbatim raw payload behind one triangulation estimate (click 2 of the audit chain)",
)
def get_triangulation_raw(
    cell_id: int,
    triangulation_id: int,
    db: DbSession,
    _user: CurrentUserDep,
) -> dict:
    """Return the verbatim ``raw_*.raw_json`` record that fed one estimate.

    This is the terminal hop of the two-click audit chain: the Cell Detail
    SourcePanel links here via "View raw payload". The row is resolved with the
    same cell-aware matching as the inline ``raw_ref`` (country / subcategory /
    HS-code filters, falling back to the source's most recent row) and returned
    inside a small provenance envelope so the payload is self-describing.
    """
    tri = (
        db.execute(
            select(CellTriangulation)
            .options(
                joinedload(CellTriangulation.source),
                joinedload(CellTriangulation.method),
            )
            .where(CellTriangulation.triangulation_id == triangulation_id)
        )
        .unique()
        .scalars()
        .first()
    )
    if tri is None or tri.cell_id != cell_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Triangulation {triangulation_id} not found for cell {cell_id}.",
        )
    if tri.source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Triangulation {triangulation_id} carries no source record.",
        )

    cell = db.execute(
        select(Cell)
        .options(joinedload(Cell.subcategory), joinedload(Cell.geography))
        .where(Cell.cell_id == cell_id)
    ).unique().scalars().first()

    raw_ref = _resolve_raw_ref(db, tri.source, cell)
    if raw_ref.raw_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No raw payload ingested yet for source {tri.source_id!r} "
                f"(table {raw_ref.raw_table or 'unknown'!r})."
            ),
        )

    return {
        "cell_id": cell_id,
        "triangulation_id": triangulation_id,
        "method_code": tri.method_code,
        "estimate_usd_m": tri.estimate_usd_m,
        "source_id": tri.source_id,
        "publisher": tri.source.publisher,
        "raw_table": raw_ref.raw_table,
        "raw_id": raw_ref.raw_id,
        "accessed_at": raw_ref.accessed_at,
        "raw_json": raw_ref.sample,
    }


# ---------------------------------------------------------------------------
# GET /api/cells/{cell_id}/triangulation-summary
# ---------------------------------------------------------------------------

@router.get(
    "/{cell_id}/triangulation-summary",
    response_model=TriangulationSummaryOut,
    summary="Confidence math for one cell (cell_triangulation_summary view)",
)
def get_triangulation_summary(
    cell_id: int,
    db: DbSession,
    _user: CurrentUserDep,
) -> TriangulationSummaryOut:
    """Return confidence math for one cell from the ``cell_triangulation_summary`` view.

    The materialised view computes (Q5):

    * ``n_distinct_methods`` — ``COUNT(DISTINCT method_code)``
    * ``n_independent_signals`` — ``COUNT(DISTINCT (method_code, source_class))``
    * ``spread_ratio`` — ``(max - min) / median``
    * ``qualifies_high`` / ``qualifies_medium`` — boolean verdicts against the
      **ACTIVE** validation profile thresholds (Standard by default)
    * ``effective_signals`` — the signal count that the active profile's
      ``independence_level`` (``method`` | ``method_x_source_class``) actually uses

    Returns 404 when no summary row exists, which means either the cell does not
    exist or the pipeline has not yet written any estimates for it (or the view
    has not been refreshed). The parent ``GET /api/cells/{id}`` endpoint indicates
    the same state via ``summary: null``.
    """
    # Fast primary-key lookup on the unique index of the materialised view.
    row = db.get(CellTriangulationSummary, cell_id)
    if row is None:
        # Distinguish "cell genuinely missing" from "no estimates yet" with one
        # extra lightweight check.
        cell_exists = db.execute(
            select(func.count()).select_from(Cell).where(Cell.cell_id == cell_id)
        ).scalar_one()
        if not cell_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Cell {cell_id} not found.",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No triangulation data for cell {cell_id}. "
                "The cell exists but has no estimates yet, or the "
                "cell_triangulation_summary view has not been refreshed."
            ),
        )

    return TriangulationSummaryOut.model_validate(row)
