"""Export router — ``GET /api/export/xlsx``.

Generates and streams an XLSX workbook for one of five export flavors:

``cell_explorer``
    Filterable cells (subcategory × geography × year), TAM band, confidence chip.

``cell_detail``
    Full audit drill chain: one row per method estimate → source (hyperlinked) →
    raw-table reference.  Optionally scoped to a single cell via ``cell_id``.

``player_shares``
    Ranked company market shares + buyer-supplier relationship edges.

``triangulation``
    Per-method estimates + confidence-math projection from the
    ``cell_triangulation_summary`` materialised view.

``assumptions``
    Versioned assumption ledger + cell–assumption bridge table (reverse drill).

Every workbook includes a ``_README`` sheet recording the filter scope, export
timestamp, methodology hyperlink, and the four spec invariants.  Source URLs in
the data sheets are rendered as clickable hyperlinks.

Auth: all four roles (owner, admin, analyst, business, external) may export.
Generating very large exports (no filters, full dataset) is permitted but may
take several seconds; the response streams the XLSX as a single chunk so the
browser triggers the download as soon as the workbook is built.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from backend.app.deps import CurrentUserDep, DbSession
from backend.app.services.export_xlsx import ExportFlavor, build_workbook, workbook_to_bytes

logger = logging.getLogger("grx10.routers.export")

router = APIRouter(prefix="/export", tags=["export"])

# ---------------------------------------------------------------------------
# Accepted flavor values (validated in-place; no Enum to keep query-param docs
# readable in the OpenAPI UI).
# ---------------------------------------------------------------------------
_VALID_FLAVORS: frozenset[str] = frozenset(
    {"cell_explorer", "cell_detail", "player_shares", "triangulation", "assumptions"}
)

_CONFIDENCE_VALUES: frozenset[str] = frozenset({"high", "medium", "low"})

_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ---------------------------------------------------------------------------
# GET /api/export/xlsx
# ---------------------------------------------------------------------------

@router.get(
    "/xlsx",
    summary="Export XLSX — five flavors (Cell Explorer, Cell Detail, Player Shares, Triangulation, Assumptions)",
    description=(
        "Streams an XLSX workbook for the requested *flavor*.  Every workbook "
        "contains a ``_README`` sheet (filter scope, export timestamp, methodology "
        "link) and hyperlinked source URLs.  All four roles may call this endpoint."
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "XLSX file download",
            "content": {
                _MIME: {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        422: {"description": "Invalid flavor or filter parameter"},
    },
)
def export_xlsx(
    db: DbSession,
    _user: CurrentUserDep,
    flavor: Annotated[
        str,
        Query(
            description=(
                "Export flavor: "
                "cell_explorer | cell_detail | player_shares | "
                "triangulation | assumptions"
            )
        ),
    ] = "cell_explorer",
    cell_id: Annotated[
        int | None,
        Query(
            description=(
                "Scope cell_detail to a single cell.  "
                "Also accepted by player_shares and triangulation to scope one cell.  "
                "Ignored by cell_explorer and assumptions."
            )
        ),
    ] = None,
    subcategory_id: Annotated[
        int | None,
        Query(description="Filter by taxonomy subcategory_id."),
    ] = None,
    geography_id: Annotated[
        int | None,
        Query(description="Filter by geography_id."),
    ] = None,
    year: Annotated[
        int | None,
        Query(description="Filter by calendar year."),
    ] = None,
    confidence: Annotated[
        str | None,
        Query(description="Filter by confidence band: high | medium | low"),
    ] = None,
) -> StreamingResponse:
    """Stream an XLSX workbook for *flavor* with optional cell-level filters.

    The workbook is built synchronously in this request; for large exports
    (unfiltered full dataset) expect 1–10 seconds.  The response is a single
    streaming chunk that triggers the browser download dialog.

    Filter semantics
    ----------------
    * Filters are ANDed.  No filter = full dataset.
    * ``cell_id`` additionally scopes ``cell_detail``, ``player_shares``, and
      ``triangulation`` to a single cell's data.
    * For ``assumptions``, ``subcategory_id`` and ``geography_id`` filter by
      ``scope_subcategory_id`` / ``scope_geography_id`` on the assumption row.

    Invariants preserved
    --------------------
    * No export row is synthesised: all values come from the DB.
    * Source URLs are from ``sources.url_pattern``; if absent the cell is left
      unlinked (never fabricated).
    * Confidence colouring reflects the stored ``confidence`` field which is
      itself written only by the pipeline from the materialised view.
    """
    # --- Validate query params ---------------------------------------------- #
    if flavor not in _VALID_FLAVORS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unknown flavor {flavor!r}. "
                f"Must be one of: {', '.join(sorted(_VALID_FLAVORS))}."
            ),
        )

    if confidence is not None and confidence not in _CONFIDENCE_VALUES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid confidence {confidence!r}. "
                f"Must be one of: {', '.join(sorted(_CONFIDENCE_VALUES))}."
            ),
        )

    if year is not None and not (1900 <= year <= 2200):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"year {year} is outside the accepted range 1900–2200.",
        )

    # --- Build workbook ----------------------------------------------------- #
    logger.info(
        "export_xlsx flavor=%s cell_id=%s subcategory_id=%s geography_id=%s "
        "year=%s confidence=%s user=%s",
        flavor, cell_id, subcategory_id, geography_id, year, confidence,
        _user.id,
    )

    try:
        wb = build_workbook(
            flavor,  # type: ignore[arg-type]
            db,
            cell_id=cell_id,
            subcategory_id=subcategory_id,
            geography_id=geography_id,
            year=year,
            confidence=confidence,
        )
    except ValueError as exc:
        # build_workbook raises ValueError for unknown flavors; guard just in case
        # the validation above is bypassed (e.g. mypy cast in tests).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("export_xlsx build failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build the export workbook. Check server logs.",
        ) from exc

    # --- Serialize and stream ----------------------------------------------- #
    xlsx_bytes = workbook_to_bytes(wb)
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"grx10_{flavor}_{ts_tag}.xlsx"

    logger.info(
        "export_xlsx flavor=%s size_bytes=%d filename=%s",
        flavor, len(xlsx_bytes), filename,
    )

    return StreamingResponse(
        content=iter([xlsx_bytes]),
        media_type=_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(xlsx_bytes)),
            "Cache-Control": "no-store, no-cache",
        },
    )
