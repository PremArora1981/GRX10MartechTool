"""Layer 3 — Players router.

Serves read-only views of player shares and supplier relationships for a cell.
Both endpoints satisfy the spec's two-click drill requirement: ``source_id`` is
always present on every row so the UI can link to the source record and then to
the raw payload without further round-trips.

Routes (auto-registered by ``main.py`` router discovery):
    GET /cells/{cell_id}/players
        Ranked player_shares for a cell, optionally filtered by ``player_role``.
        Each row carries the full ``company`` object (joined) so the Players
        screen renders in one fetch.

    GET /cells/{cell_id}/supplier-relationships
        Buyer/supplier edges from ``supplier_relationships`` scoped to a cell.
        Both ``buyer`` and ``supplier`` company objects are joined inline.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.orm import joinedload

from backend.app.deps import CurrentUserDep, DbSession
from backend.app.models import Cell, PlayerShare, SupplierRelationship
from backend.app.schemas import (
    PlayerShareList,
    PlayerShareOut,
    SupplierRelationshipList,
    SupplierRelationshipOut,
)

logger = logging.getLogger("grx10.routers.players")

router = APIRouter(prefix="/cells", tags=["players"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _require_cell(cell_id: int, session) -> Cell:
    """Return the Cell row or raise HTTP 404.

    Keeps endpoint handlers clean: every handler calls this once at the top
    so callers receive a meaningful 404 rather than an empty result set when the
    cell_id is wrong.
    """
    cell = session.get(Cell, cell_id)
    if cell is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cell {cell_id!r} not found.",
        )
    return cell


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@router.get(
    "/{cell_id}/players",
    response_model=PlayerShareList,
    summary="Ranked player shares for a cell",
)
def list_player_shares(
    cell_id: int,
    session: DbSession,
    _user: CurrentUserDep,
    player_role: Annotated[
        str | None,
        Query(
            description=(
                "Restrict to a single role, e.g. 'producer', 'distributor', 'supplier', "
                "'buyer', 'OEM', 'CDMO'. Omit to return all roles."
            )
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Page size (max 200).")] = 50,
    offset: Annotated[int, Query(ge=0, description="Zero-based row offset.")] = 0,
) -> PlayerShareList:
    """Return ``player_shares`` rows for *cell_id*, ordered by role then rank.

    Each returned item carries:
    * ``share_pct`` + band (``share_low_pct`` / ``share_high_pct``) — always
      show bands where available (acceptance criterion mirrors the TAM band rule).
    * ``revenue_usd_m`` — derived from the cell TAM × share if computed.
    * ``confidence`` — set on the share row by the pipeline, never hard-overridden.
    * ``company`` — the full company object (name, type, HQ country) for display.
    * ``source_id`` — present on every row (invariant); links to the drill chain.

    Returns an empty ``items`` list (not 404) if the cell has no player data yet.
    """
    _require_cell(cell_id, session)

    q = (
        session.query(PlayerShare)
        .options(joinedload(PlayerShare.company))
        .filter(PlayerShare.cell_id == cell_id)
    )
    if player_role is not None:
        q = q.filter(PlayerShare.player_role == player_role)

    total: int = q.count()
    rows = (
        q.order_by(PlayerShare.player_role, PlayerShare.rank)
        .offset(offset)
        .limit(limit)
        .all()
    )

    logger.debug(
        "list_player_shares cell=%d role=%s total=%d returned=%d",
        cell_id, player_role, total, len(rows),
    )

    return PlayerShareList(
        items=[PlayerShareOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        cell_id=cell_id,
    )


@router.get(
    "/{cell_id}/supplier-relationships",
    response_model=SupplierRelationshipList,
    summary="Supplier relationships scoped to a cell",
)
def list_supplier_relationships(
    cell_id: int,
    session: DbSession,
    _user: CurrentUserDep,
    limit: Annotated[int, Query(ge=1, le=200, description="Page size (max 200).")] = 50,
    offset: Annotated[int, Query(ge=0, description="Zero-based row offset.")] = 0,
) -> SupplierRelationshipList:
    """Return ``supplier_relationships`` edges whose ``cell_id`` matches.

    Engagement-level edges (``cell_id IS NULL``) are intentionally excluded here
    — they span the whole engagement and belong on an engagement-level endpoint
    when built.  Rows are ordered by ``evidence_strength`` descending (strongest
    evidence first) then by ``relationship_id`` for a stable sort.

    Each edge carries:
    * ``buyer`` / ``supplier`` — full company objects (joined) for display.
    * ``relationship_type`` — e.g. 'tier1_supplier', 'oem_contract'.
    * ``evidence_type`` / ``evidence_strength`` — provenance metadata.
    * ``source_id`` — drill-chain anchor (invariant: every fact row has one).
    """
    _require_cell(cell_id, session)

    q = (
        session.query(SupplierRelationship)
        .options(
            joinedload(SupplierRelationship.buyer),
            joinedload(SupplierRelationship.supplier),
        )
        .filter(SupplierRelationship.cell_id == cell_id)
    )

    total: int = q.count()
    rows = (
        q.order_by(
            SupplierRelationship.evidence_strength.desc(),
            SupplierRelationship.relationship_id,
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    logger.debug(
        "list_supplier_relationships cell=%d total=%d returned=%d",
        cell_id, total, len(rows),
    )

    return SupplierRelationshipList(
        items=[SupplierRelationshipOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        cell_id=cell_id,
    )
