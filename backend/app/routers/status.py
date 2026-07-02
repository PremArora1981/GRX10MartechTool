"""Pipeline-status router — feeds the status page (spec §5 screen 8).

Answers three questions the status page needs:

1. **Connector health** — count of enabled sources in each of the 7 probe-state
   buckets (``OK | AUTH_FAILED | QUOTA_EXHAUSTED | RATE_LIMITED | UNREACHABLE |
   SCHEMA_MISMATCH | EMPTY``) plus a ``never_probed`` catch-all.

2. **Per-source freshness** — ordered list of every source with its
   ``last_probe_at``, ``last_probe_status``, staleness flag, and a budget/quota
   pre-warning flag (Q7: surface "out of money" before the pipeline blocks).

3. **Cell coverage** — total cells vs cells that have at least one triangulation
   estimate, as a percentage, broken down by confidence band.

All endpoints are **read-only**. Writes live in ``connectors/`` and the pipeline.
"""

from __future__ import annotations

import datetime
import logging
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, nullslast, select
from sqlalchemy.orm import Session

from backend.app.deps import CurrentUserDep, DbSession
from backend.app.models import Cell, CellTriangulation, Source

logger = logging.getLogger("grx10.routers.status")

router = APIRouter(prefix="/status", tags=["status"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A source is "stale" when its last probe is older than this threshold.
# Weekly cadence (from refresh_cadence) + 1 h of slack.
_STALE_HOURS: int = 24 * 7 + 1

# Budget pre-warning at 80 % of ceiling (Q7).
_BUDGET_WARNING_FRACTION: float = 0.80

# The 7-state probe taxonomy (from the connector contract).
_PROBE_STATES = frozenset(
    {
        "OK",
        "AUTH_FAILED",
        "QUOTA_EXHAUSTED",
        "RATE_LIMITED",
        "UNREACHABLE",
        "SCHEMA_MISMATCH",
        "EMPTY",
    }
)


# ---------------------------------------------------------------------------
# Response schemas (self-contained to this router — not in schemas.py, which
# carries wire contracts that other agents depend on)
# ---------------------------------------------------------------------------


class ConnectorHealthSummary(BaseModel):
    """Counts of *enabled* sources in each probe-status bucket.

    ``never_probed`` covers enabled sources with a NULL or unrecognised
    ``last_probe_status`` — i.e. connectors that have been registered but not
    yet exercised by the pipeline.
    """

    ok: int = 0
    auth_failed: int = 0
    quota_exhausted: int = 0
    rate_limited: int = 0
    unreachable: int = 0
    schema_mismatch: int = 0
    empty: int = 0
    never_probed: int = 0
    # Totals include disabled so the UI can show the full picture.
    total_enabled: int = 0
    total_disabled: int = 0


class SourceFreshnessItem(BaseModel):
    """Per-source row for the status table, enriched with computed flags."""

    source_id: str
    publisher: str
    access_method: str | None
    source_class: str | None = Field(alias="source_class", default=None)
    last_probe_status: str | None
    last_probe_at: datetime.datetime | None
    last_probe_detail: str | None
    enabled: bool
    # Derived flags.
    is_stale: bool = Field(
        description=(
            "True when the source has not been probed within the freshness window "
            f"({_STALE_HOURS} h)."
        )
    )
    budget_warning: bool = Field(
        description=(
            "True when the source has a cost/quota ceiling configured AND its last "
            "probe returned QUOTA_EXHAUSTED (Q7 pre-warning signal)."
        )
    )
    monthly_budget: float | None
    quota_ceiling: int | None

    model_config = {"populate_by_name": True}


class CellCoverageStats(BaseModel):
    """How much of the cell matrix has been filled by the sizing pipeline."""

    total_cells: int
    cells_with_estimates: int = Field(
        description="Cells with at least one ``cell_triangulation`` row."
    )
    coverage_pct: float = Field(description="Percentage (0.0 – 100.0).")
    cells_by_confidence: dict[str, int] = Field(
        description=(
            "Count of cells at each confidence band: "
            "``{high, medium, low, none}``."
        )
    )


class StatusResponse(BaseModel):
    """Top-level GET /status response — the status-page hero section."""

    pipeline_healthy: bool = Field(
        description=(
            "True when no enabled source is in AUTH_FAILED, UNREACHABLE, or "
            "QUOTA_EXHAUSTED state."
        )
    )
    last_pipeline_ok_at: datetime.datetime | None = Field(
        description="Most recent ``last_probe_at`` across sources whose status is OK."
    )
    connector_health: ConnectorHealthSummary
    cell_coverage: CellCoverageStats
    generated_at: datetime.datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_stale(last_probe_at: datetime.datetime | None) -> bool:
    """Return True when the source hasn't been probed within ``_STALE_HOURS``."""
    if last_probe_at is None:
        return True
    # DB column is TIMESTAMPTZ; guard against naive datetimes defensively.
    ts = (
        last_probe_at
        if last_probe_at.tzinfo is not None
        else last_probe_at.replace(tzinfo=datetime.timezone.utc)
    )
    age = datetime.datetime.now(datetime.timezone.utc) - ts
    return age > datetime.timedelta(hours=_STALE_HOURS)


def _budget_warning(source: Source) -> bool:
    """True when a source has a cost ceiling AND last reported QUOTA_EXHAUSTED.

    Actual spend-vs-ceiling accounting is the pipeline's responsibility.  The
    status page surfaces the probe-state signal so operators notice quota
    exhaustion before it silently blocks data ingestion.
    """
    has_ceiling = source.monthly_budget is not None or source.quota_ceiling is not None
    quota_hit = (source.last_probe_status or "").upper() == "QUOTA_EXHAUSTED"
    return has_ceiling and quota_hit


def _build_health_summary(sources: list[Source]) -> ConnectorHealthSummary:
    """Aggregate per-source probe statuses into the health-summary counts."""
    summary = ConnectorHealthSummary()
    for src in sources:
        if not src.enabled:
            summary.total_disabled += 1
            continue
        summary.total_enabled += 1
        state = (src.last_probe_status or "").upper()
        if state == "OK":
            summary.ok += 1
        elif state == "AUTH_FAILED":
            summary.auth_failed += 1
        elif state == "QUOTA_EXHAUSTED":
            summary.quota_exhausted += 1
        elif state == "RATE_LIMITED":
            summary.rate_limited += 1
        elif state == "UNREACHABLE":
            summary.unreachable += 1
        elif state == "SCHEMA_MISMATCH":
            summary.schema_mismatch += 1
        elif state == "EMPTY":
            summary.empty += 1
        else:
            # NULL or unrecognised value — connector registered but not yet probed.
            summary.never_probed += 1
    return summary


def _build_coverage(db: Session) -> CellCoverageStats:
    """Compute cell coverage stats from the cells and cell_triangulation tables."""
    total: int = db.scalar(select(func.count()).select_from(Cell)) or 0

    # Cells that have at least one triangulation estimate (deduped by cell_id).
    cells_with_estimates: int = (
        db.scalar(
            select(func.count(func.distinct(CellTriangulation.cell_id)))
        )
        or 0
    )

    coverage_pct: float = (
        round(cells_with_estimates / total * 100.0, 1) if total > 0 else 0.0
    )

    # Confidence distribution (cells table, column written by the pipeline after
    # reading the cell_triangulation_summary view).
    conf_rows = db.execute(
        select(Cell.confidence, func.count().label("n")).group_by(Cell.confidence)
    ).all()
    by_conf: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for conf, n in conf_rows:
        key = conf if conf in by_conf else "none"
        by_conf[key] = by_conf.get(key, 0) + (n or 0)

    return CellCoverageStats(
        total_cells=total,
        cells_with_estimates=cells_with_estimates,
        coverage_pct=coverage_pct,
        cells_by_confidence=by_conf,
    )


def _make_freshness_item(src: Source) -> SourceFreshnessItem:
    """Build a ``SourceFreshnessItem`` from a ``Source`` ORM row."""
    return SourceFreshnessItem(
        source_id=src.source_id,
        publisher=src.publisher,
        access_method=src.access_method,
        source_class=src.source_class,
        last_probe_status=src.last_probe_status,
        last_probe_at=src.last_probe_at,
        last_probe_detail=src.last_probe_detail,
        enabled=bool(src.enabled),
        is_stale=_is_stale(src.last_probe_at),
        budget_warning=_budget_warning(src),
        monthly_budget=float(src.monthly_budget) if src.monthly_budget is not None else None,
        quota_ceiling=src.quota_ceiling,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=StatusResponse,
    summary="Pipeline health snapshot",
    description=(
        "Aggregated connector-health counts + cell-coverage stats. "
        "Drives the status-page hero section."
    ),
)
def get_status(db: DbSession, _user: CurrentUserDep) -> StatusResponse:
    """Return the overall pipeline-health snapshot.

    Reads ``sources`` (health + freshness) and ``cells`` / ``cell_triangulation``
    (coverage). No writes.
    """
    sources: list[Source] = db.execute(select(Source)).scalars().all()

    health = _build_health_summary(list(sources))
    coverage = _build_coverage(db)

    # Most recent probe timestamp across sources that last came back OK.
    last_ok: datetime.datetime | None = db.scalar(
        select(func.max(Source.last_probe_at)).where(
            Source.last_probe_status == "OK"
        )
    )

    # Healthy = no enabled source is stuck in a blocking failure state.
    pipeline_healthy: bool = (
        health.auth_failed == 0
        and health.unreachable == 0
        and health.quota_exhausted == 0
    )

    return StatusResponse(
        pipeline_healthy=pipeline_healthy,
        last_pipeline_ok_at=last_ok,
        connector_health=health,
        cell_coverage=coverage,
        generated_at=datetime.datetime.now(datetime.timezone.utc),
    )


@router.get(
    "/sources",
    response_model=list[SourceFreshnessItem],
    summary="Per-source freshness list",
    description=(
        "Ordered by ``last_probe_at`` descending (most-recent first, never-probed "
        "last). Includes staleness and budget-warning flags."
    ),
)
def list_source_freshness(
    db: DbSession,
    _user: CurrentUserDep,
    enabled_only: bool = False,
) -> list[SourceFreshnessItem]:
    """Return per-source freshness with staleness + budget-warning flags.

    Args:
        enabled_only: When True, suppress disabled sources from the response.
    """
    q = select(Source)
    if enabled_only:
        q = q.where(Source.enabled.is_(True))
    q = q.order_by(nullslast(Source.last_probe_at.desc()))

    sources: list[Source] = db.execute(q).scalars().all()
    return [_make_freshness_item(s) for s in sources]


@router.get(
    "/coverage",
    response_model=CellCoverageStats,
    summary="Cell coverage breakdown",
    description=(
        "Total cells vs cells-with-estimates, percentage, and confidence-band "
        "distribution. Isolates coverage from the full status snapshot."
    ),
)
def get_cell_coverage(db: DbSession, _user: CurrentUserDep) -> CellCoverageStats:
    """Return cell-coverage stats broken down by confidence band."""
    return _build_coverage(db)
