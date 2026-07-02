"""Reports router — PDF report generation + the Excel-export bridge.

Implements the frontend contract (see ``frontend/lib/api.ts``):

* ``POST /reports/{type}``         -> ``ReportResult`` (download link)
* ``POST /reports/custom``         -> ``ReportResult``
* ``POST /exports/excel/{flavor}`` -> ``ReportResult`` (links to the streaming
  ``GET /api/export/xlsx`` owned by ``routers/export.py``)
* ``GET  /reports/{type}/download``   -> streams the PDF
* ``GET  /reports/custom/download``   -> streams a custom PDF

Two-step by design: the POST returns a JSON ``ReportResult`` whose
``download_url`` the browser then opens to stream the file. Because the frontend
and backend are separate origins on Render, ``download_url`` is **absolute**,
built from ``request.base_url`` so it always points back at this API.

The frontend uses hyphenated report types (``executive-audit``) and its own
Excel-flavour names (``cells``/``players``/``full``); both are translated here to
the underscored service/router vocabulary. PDF building lives in
``services/reports_pdf.py``; every report ends with a numbered Sources page and
shows TAM bands + confidence — invariants enforced in that service.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.app.deps import CurrentUserDep, DbSession
from backend.app.services import reports_pdf

logger = logging.getLogger("grx10.routers.reports")

router = APIRouter(tags=["reports"])

_PDF_MIME = "application/pdf"

# Frontend (hyphenated) -> PDF builder function.
_STANDARD_BUILDERS = {
    "executive-audit": reports_pdf.build_executive_audit,
    "gap-analysis": reports_pdf.build_gap_analysis,
    "player-shares": reports_pdf.build_player_shares,
}

# Frontend ExcelFlavor -> backend export-router flavour (routers/export.py).
# "full" has no dedicated backend flavour yet; it falls back to cell_explorer.
_EXCEL_FLAVOR_MAP = {
    "cells": "cell_explorer",
    "triangulation": "triangulation",
    "players": "player_shares",
    "assumptions": "assumptions",
    "full": "cell_explorer",
}


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class ReportParams(BaseModel):
    """Filters shared by every standard report + Excel export."""

    year: int | None = None
    subcategory_ids: list[int] | None = None
    geography_ids: list[int] | None = None
    confidence: str | None = None


class CustomReportParams(BaseModel):
    """Body for the custom report builder (cart-style section assembly)."""

    sections: list[str] = Field(default_factory=list)
    year: int | None = None
    subcategory_ids: list[int] | None = None
    geography_ids: list[int] | None = None
    title: str = "Custom Report"


class ReportResult(BaseModel):
    """Mirrors the frontend ``ReportResult`` interface exactly."""

    download_url: str
    generated_at: str
    report_type: str
    expires_at: str | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _abs(request: Request, path: str, params: dict[str, object]) -> str:
    """Build an absolute backend URL with a query string (skips None/empty)."""
    base = str(request.base_url).rstrip("/")
    pairs: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                pairs.append(f"{key}={item}")
        else:
            pairs.append(f"{key}={value}")
    query = ("?" + "&".join(pairs)) if pairs else ""
    return f"{base}/{path.lstrip('/')}{query}"


def _ids(raw: str | None) -> list[int] | None:
    """Parse a comma-separated id list from a query param."""
    if not raw:
        return None
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if piece:
            out.append(int(piece))
    return out or None


def _stream_pdf(buf, filename: str) -> StreamingResponse:
    data = buf.getvalue()
    return StreamingResponse(
        content=iter([data]),
        media_type=_PDF_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------- #
# Standard PDF reports
# --------------------------------------------------------------------------- #
@router.post("/reports/{report_type}", response_model=ReportResult)
def generate_standard_report(
    report_type: str,
    body: ReportParams,
    request: Request,
    _user: CurrentUserDep,
) -> ReportResult:
    """Return a download link for one of the three standard reports."""
    if report_type not in _STANDARD_BUILDERS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown report type {report_type!r}. "
            f"Valid: {', '.join(_STANDARD_BUILDERS)}.",
        )
    download_url = _abs(
        request,
        f"reports/{report_type}/download",
        {
            "year": body.year,
            "subcategory_ids": ",".join(map(str, body.subcategory_ids)) if body.subcategory_ids else None,
            "geography_ids": ",".join(map(str, body.geography_ids)) if body.geography_ids else None,
            "confidence": body.confidence,
        },
    )
    logger.info("standard report requested type=%s user=%s", report_type, _user.id)
    return ReportResult(
        download_url=download_url,
        generated_at=_now_iso(),
        report_type=report_type,
        expires_at=None,
    )


@router.get("/reports/{report_type}/download", response_class=StreamingResponse)
def download_standard_report(
    report_type: str,
    db: DbSession,
    _user: CurrentUserDep,
    year: int | None = Query(None),
    subcategory_ids: str | None = Query(None),
    geography_ids: str | None = Query(None),
    confidence: str | None = Query(None),
) -> StreamingResponse:
    """Build and stream the PDF for a standard report."""
    builder = _STANDARD_BUILDERS.get(report_type)
    if builder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown report type.")
    try:
        buf = builder(
            db,
            subcategory_ids=_ids(subcategory_ids),
            geography_ids=_ids(geography_ids),
            year=year,
            confidence=confidence,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("PDF build failed: %s", exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build the report PDF. Check server logs.",
        ) from exc
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _stream_pdf(buf, f"grx10_{report_type.replace('-', '_')}_{ts}.pdf")


# --------------------------------------------------------------------------- #
# Custom report builder
# --------------------------------------------------------------------------- #
@router.post("/reports/custom", response_model=ReportResult)
def generate_custom_report(
    body: CustomReportParams,
    request: Request,
    _user: CurrentUserDep,
) -> ReportResult:
    """Return a download link for a custom (cart-assembled) report."""
    if not body.sections:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one section is required for a custom report.",
        )
    download_url = _abs(
        request,
        "reports/custom/download",
        {
            "sections": ",".join(body.sections),
            "year": body.year,
            "subcategory_ids": ",".join(map(str, body.subcategory_ids)) if body.subcategory_ids else None,
            "geography_ids": ",".join(map(str, body.geography_ids)) if body.geography_ids else None,
            "title": body.title,
        },
    )
    return ReportResult(
        download_url=download_url,
        generated_at=_now_iso(),
        report_type="custom",
        expires_at=None,
    )


@router.get("/reports/custom/download", response_class=StreamingResponse)
def download_custom_report(
    db: DbSession,
    _user: CurrentUserDep,
    sections: str = Query(..., description="Comma-separated section names"),
    year: int | None = Query(None),
    subcategory_ids: str | None = Query(None),
    geography_ids: str | None = Query(None),
    title: str = Query("Custom Report"),
) -> StreamingResponse:
    """Build and stream a custom PDF from the requested ordered sections."""
    section_list = [s.strip() for s in sections.split(",") if s.strip()]
    try:
        buf = reports_pdf.build_custom(
            db,
            sections=section_list,
            subcategory_ids=_ids(subcategory_ids),
            geography_ids=_ids(geography_ids),
            year=year,
            title=title,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("custom PDF build failed: %s", exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build the custom report PDF.",
        ) from exc
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _stream_pdf(buf, f"grx10_custom_{ts}.pdf")


# --------------------------------------------------------------------------- #
# Excel export bridge -> GET /api/export/xlsx (routers/export.py)
# --------------------------------------------------------------------------- #
@router.post("/exports/excel/{flavor}", response_model=ReportResult)
def generate_excel_export(
    flavor: str,
    body: ReportParams,
    request: Request,
    _user: CurrentUserDep,
) -> ReportResult:
    """Return a download link to the streaming XLSX endpoint for *flavor*."""
    backend_flavor = _EXCEL_FLAVOR_MAP.get(flavor)
    if backend_flavor is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown Excel flavor {flavor!r}. "
            f"Valid: {', '.join(_EXCEL_FLAVOR_MAP)}.",
        )
    download_url = _abs(
        request,
        "export/xlsx",
        {
            "flavor": backend_flavor,
            "year": body.year,
            "subcategory_id": body.subcategory_ids[0] if body.subcategory_ids else None,
            "geography_id": body.geography_ids[0] if body.geography_ids else None,
            "confidence": body.confidence,
        },
    )
    return ReportResult(
        download_url=download_url,
        generated_at=_now_iso(),
        report_type=f"excel:{flavor}",
        expires_at=None,
    )
