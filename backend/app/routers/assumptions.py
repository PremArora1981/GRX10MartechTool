"""Versioned assumptions ledger (Layer 4 — Decisions).

Spec invariant (§ acceptance criteria): assumption state is **never overwritten**.
Every POST creates a new row and wires the previous active head (if any) via its
``superseded_by`` column, producing an immutable append-only audit chain::

    A1 (superseded_by=A2) → A2 (superseded_by=A3) → A3  ← active head

The scope of an assumption is the conjunction of its three nullable scope
columns.  Two assumptions share a scope when all three columns match
(NULL == NULL semantics — IS NOT DISTINCT FROM).

Routes (auto-registered by ``main.py`` router discovery):
    GET  /assumptions                         — paginated list; active_only hides superseded
    POST /assumptions                         — create + auto-supersede the prior head
    GET  /assumptions/{assumption_id}         — single assumption detail
    GET  /assumptions/{assumption_id}/cells   — reverse drill: cells influenced by this assumption
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.deps import CurrentUserDep, DbSession, require_role
from backend.app.models import (
    Assumption,
    Cell,
    CellAssumptionLink,
    Geography,
    TaxonomySubcategory,
)
from backend.app.schemas import (
    AssumptionCreate,
    AssumptionInfluencedCells,
    AssumptionList,
    AssumptionOut,
    CellSummary,
)

logger = logging.getLogger("grx10.routers.assumptions")

router = APIRouter(prefix="/assumptions", tags=["assumptions"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _scope_conditions(
    scope_company_id: int | None,
    scope_subcategory_id: int | None,
    scope_geography_id: int | None,
) -> list:
    """Build IS-NOT-DISTINCT-FROM filter clauses for the three nullable scope columns.

    Standard SQLAlchemy equality (``==``) treats NULL as unknown, so it cannot
    match NULL == NULL.  We need ``IS`` for null-to-null matches and ``==`` for
    non-null-to-non-null, which gives us the semantics of PostgreSQL's
    ``IS NOT DISTINCT FROM``.  This ensures a global assumption (all scopes
    null) only supersedes other global assumptions, not company-scoped ones.
    """
    conds: list = []

    if scope_company_id is None:
        conds.append(Assumption.scope_company_id.is_(None))
    else:
        conds.append(Assumption.scope_company_id == scope_company_id)

    if scope_subcategory_id is None:
        conds.append(Assumption.scope_subcategory_id.is_(None))
    else:
        conds.append(Assumption.scope_subcategory_id == scope_subcategory_id)

    if scope_geography_id is None:
        conds.append(Assumption.scope_geography_id.is_(None))
    else:
        conds.append(Assumption.scope_geography_id == scope_geography_id)

    return conds


def _require_assumption(assumption_id: int, session: Session) -> Assumption:
    """Return the Assumption row or raise HTTP 404.

    Superseded assumptions remain accessible — this function never filters by
    ``superseded_by``; the full version history is always readable.
    """
    row = session.get(Assumption, assumption_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assumption {assumption_id!r} not found.",
        )
    return row


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@router.get(
    "",
    response_model=AssumptionList,
    summary="List versioned assumptions",
)
def list_assumptions(
    session: DbSession,
    _user: CurrentUserDep,
    active_only: Annotated[
        bool,
        Query(
            description=(
                "When true (default) return only current heads (``superseded_by IS NULL``). "
                "Set false to include the full version history for audit purposes."
            )
        ),
    ] = True,
    scope_company_id: Annotated[
        int | None,
        Query(description="Filter by company scope. Omit to return all company scopes."),
    ] = None,
    scope_subcategory_id: Annotated[
        int | None,
        Query(description="Filter by subcategory scope."),
    ] = None,
    scope_geography_id: Annotated[
        int | None,
        Query(description="Filter by geography scope."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Page size (max 200).")] = 50,
    offset: Annotated[int, Query(ge=0, description="Zero-based row offset.")] = 0,
) -> AssumptionList:
    """Paginated assumptions ledger.

    Default (``active_only=true``) returns only the current head of each version
    chain — the assumptions actually in effect.  Passing ``active_only=false``
    exposes the full immutable history so reviewers can see every change and when
    it was superseded.

    The scope filters are *inclusive narrowing* — each supplied filter adds an
    ``AND`` condition using regular equality.  To find global (un-scoped)
    assumptions, omit all scope parameters and the result will include them (when
    ``active_only=false`` you get the full history including global ones).
    """
    q = session.query(Assumption)

    if active_only:
        q = q.filter(Assumption.superseded_by.is_(None))

    # Scope filters — inclusive, not IS-NOT-DISTINCT-FROM: the caller is
    # deliberately narrowing, so NULL scope columns on the row are excluded
    # when a concrete filter value is given.
    if scope_company_id is not None:
        q = q.filter(Assumption.scope_company_id == scope_company_id)
    if scope_subcategory_id is not None:
        q = q.filter(Assumption.scope_subcategory_id == scope_subcategory_id)
    if scope_geography_id is not None:
        q = q.filter(Assumption.scope_geography_id == scope_geography_id)

    total: int = q.count()
    rows = (
        q.order_by(Assumption.created_at.desc(), Assumption.assumption_id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return AssumptionList(
        items=[AssumptionOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=AssumptionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new versioned assumption (auto-supersedes the prior head)",
    dependencies=[Depends(require_role("owner", "admin", "analyst"))],
)
def create_assumption(
    body: AssumptionCreate,
    session: DbSession,
    user: CurrentUserDep,
) -> AssumptionOut:
    """Create a new assumption row and automatically wire ``superseded_by`` on the prior.

    **Versioning protocol** — never overwrite, always append:

    1. A new ``assumptions`` row is inserted from the request body.
    2. The DB flushes to materialise the new ``assumption_id``.
    3. The *current head* for this scope is located: an assumption with
       identical scope columns (IS-NOT-DISTINCT-FROM semantics) and no
       ``superseded_by`` (excluding the row just created).
    4. If a prior head exists, **only** its ``superseded_by`` pointer is updated
       to reference the new row.  No other column on the prior is touched.
    5. The transaction commits atomically.

    A ``SELECT … FOR UPDATE`` is taken on the prior row to serialise concurrent
    POSTs against the same scope; the second writer will find the first's new
    row as the prior after the first transaction commits.

    Requires ``analyst``, ``admin``, or ``owner`` role.
    """
    new_row = Assumption(
        scope_company_id=body.scope_company_id,
        scope_subcategory_id=body.scope_subcategory_id,
        scope_geography_id=body.scope_geography_id,
        assumption_text=body.assumption_text,
        numeric_value=body.numeric_value,
        unit=body.unit,
        confidence=body.confidence,
        derivation_method=body.derivation_method,
        source_id=body.source_id,
        effective_from_year=body.effective_from_year,
        effective_to_year=body.effective_to_year,
        # superseded_by intentionally left null — this is the new head.
    )
    session.add(new_row)
    # Flush so the PK is assigned before we query for the prior head.
    session.flush()

    # Locate the current active head for this scope (row-level lock to
    # serialise concurrent POSTs against the same scope).
    prior: Assumption | None = (
        session.query(Assumption)
        .filter(
            *_scope_conditions(
                body.scope_company_id,
                body.scope_subcategory_id,
                body.scope_geography_id,
            ),
            Assumption.superseded_by.is_(None),
            # Exclude the row we just created (shares scope but is the new head).
            Assumption.assumption_id != new_row.assumption_id,
        )
        .with_for_update()
        .one_or_none()
    )

    if prior is not None:
        logger.info(
            "assumption %d supersedes prior %d "
            "(scope company=%s subcat=%s geo=%s) by user %s",
            new_row.assumption_id,
            prior.assumption_id,
            body.scope_company_id,
            body.scope_subcategory_id,
            body.scope_geography_id,
            user.id,
        )
        # Only update the pointer — never touch any other column on the prior.
        prior.superseded_by = new_row.assumption_id
    else:
        logger.info(
            "assumption %d created as new head for scope company=%s subcat=%s geo=%s by user %s",
            new_row.assumption_id,
            body.scope_company_id,
            body.scope_subcategory_id,
            body.scope_geography_id,
            user.id,
        )

    # Commit flushes both the insert and (if applicable) the prior's pointer update.
    session.commit()
    session.refresh(new_row)
    return AssumptionOut.model_validate(new_row)


@router.get(
    "/{assumption_id}",
    response_model=AssumptionOut,
    summary="Get a single assumption by ID",
)
def get_assumption(
    assumption_id: int,
    session: DbSession,
    _user: CurrentUserDep,
) -> AssumptionOut:
    """Return a single assumption by primary key.

    Both active and superseded assumptions are readable — the full version
    history is always accessible.  To navigate the chain, follow
    ``superseded_by`` forward (newer) or use the list endpoint with
    ``active_only=false`` filtered by scope.
    """
    row = _require_assumption(assumption_id, session)
    return AssumptionOut.model_validate(row)


@router.get(
    "/{assumption_id}/cells",
    response_model=AssumptionInfluencedCells,
    summary="Cells influenced by an assumption (reverse drill)",
)
def list_influenced_cells(
    assumption_id: int,
    session: DbSession,
    _user: CurrentUserDep,
    limit: Annotated[int, Query(ge=1, le=200, description="Page size (max 200).")] = 50,
    offset: Annotated[int, Query(ge=0, description="Zero-based row offset.")] = 0,
) -> AssumptionInfluencedCells:
    """Reverse drill: return every cell linked to this assumption via ``cell_assumption_link``.

    This powers the Assumptions Ledger screen's "influenced cells" panel (spec
    §5 — screen 6).  Each cell in the result carries:
    * ``subcategory_name`` — human-readable label from ``taxonomy_subcategories``.
    * ``country`` / ``segment`` — from ``geographies``.
    * TAM band (``tam_low_usd_m`` / ``tam_revenue_usd_m`` / ``tam_high_usd_m``).
    * ``confidence`` chip.

    All of these are joined inline so the UI can render the full list without
    further fetches.  Cells are ordered newest year first, then by ``cell_id``
    for a stable deterministic sort.
    """
    # Ensure the assumption exists — return 404 early rather than an empty list.
    _require_assumption(assumption_id, session)

    # Count separately (avoids wrapping a multi-entity SELECT in a subquery
    # that may produce an ambiguous column count).
    total: int = (
        session.query(func.count(CellAssumptionLink.cell_id))
        .filter(CellAssumptionLink.assumption_id == assumption_id)
        .scalar()
        or 0
    )

    rows = (
        session.query(Cell, TaxonomySubcategory, Geography)
        .join(CellAssumptionLink, CellAssumptionLink.cell_id == Cell.cell_id)
        .join(
            TaxonomySubcategory,
            Cell.subcategory_id == TaxonomySubcategory.subcategory_id,
        )
        .join(Geography, Cell.geography_id == Geography.geography_id)
        .filter(CellAssumptionLink.assumption_id == assumption_id)
        .order_by(Cell.year.desc(), Cell.cell_id)
        .offset(offset)
        .limit(limit)
        .all()
    )

    items: list[CellSummary] = []
    for cell, subcat, geo in rows:
        items.append(
            CellSummary(
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
                # Denormalised labels — filled from the joined rows.
                subcategory_name=subcat.name,
                country=geo.country,
                segment=geo.segment,
            )
        )

    logger.debug(
        "list_influenced_cells assumption=%d total=%d returned=%d",
        assumption_id, total, len(items),
    )

    return AssumptionInfluencedCells(
        assumption_id=assumption_id,
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )
