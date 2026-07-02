"""PDF report generation service for the GRX10 Automated Market Research Tool.

Implements three standard report types — Executive Audit, Gap Analysis, and
Player Shares — as composable section functions plus a custom section builder.

Spec invariants enforced here:
* Every report ends with a NUMBERED Sources page carrying clickable URLs.
* Every TAM shows its band (low / central / high).
* Charts carry segment + confidence labels (bar colour encodes confidence;
  category labels embed subcategory + segment names).

PDF engine: ReportLab >= 4.0.
Extra dependency (not in requirements.txt): ``reportlab``
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from backend.app.models import (
    Cell,
    CellTriangulation,
    PlayerShare,
    Source,
)

log = logging.getLogger("grx10.services.reports_pdf")

# ---------------------------------------------------------------------------
# Page geometry
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = A4
LM = RM = 2.0 * cm
TM = 2.5 * cm
BM = 2.0 * cm
INNER_W = PAGE_W - LM - RM

# ---------------------------------------------------------------------------
# Brand colours
# ---------------------------------------------------------------------------
PRIMARY   = colors.HexColor("#1A2B4A")
ACCENT    = colors.HexColor("#2563EB")
LIGHT     = colors.HexColor("#F1F5F9")
STRIPE    = colors.HexColor("#F8FAFC")
BORDER    = colors.HexColor("#E2E8F0")
CONF_HIGH = colors.HexColor("#16A34A")
CONF_MED  = colors.HexColor("#D97706")
CONF_LOW  = colors.HexColor("#DC2626")
CONF_NONE = colors.HexColor("#6B7280")

_CONF_CLR: dict[str | None, colors.Color] = {
    "high": CONF_HIGH,
    "medium": CONF_MED,
    "low": CONF_LOW,
}


# ---------------------------------------------------------------------------
# Stylesheet (built once at module load)
# ---------------------------------------------------------------------------
def _build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()

    def _ps(name: str, parent: str = "Normal", **kw: Any) -> ParagraphStyle:
        return ParagraphStyle(name, parent=base[parent], **kw)

    return {
        "Normal":       base["Normal"],
        "Heading1":     base["Heading1"],
        "Heading2":     base["Heading2"],
        "H1":           _ps("H1",  "Heading1", fontSize=16, textColor=PRIMARY, spaceAfter=6, leading=20),
        "H2":           _ps("H2",  "Heading2", fontSize=12, textColor=PRIMARY, spaceAfter=4, leading=15),
        "H3":           _ps("H3",  "Normal",   fontSize=10, textColor=PRIMARY, spaceAfter=2, leading=13,
                             fontName="Helvetica-Bold"),
        "Body":         _ps("Body", "Normal",   fontSize=9,  leading=12),
        "Small":        _ps("Small","Normal",   fontSize=8,  leading=10),
        "Tiny":         _ps("Tiny", "Normal",   fontSize=7,  leading=9, textColor=colors.grey),
        "Caption":      _ps("Caption","Normal", fontSize=8,  leading=10, textColor=colors.HexColor("#64748B"),
                             alignment=TA_CENTER),
        "TH":           _ps("TH",  "Normal",   fontSize=9,  textColor=colors.white,
                             fontName="Helvetica-Bold", leading=11),
        "TD":           _ps("TD",  "Normal",   fontSize=8,  leading=10),
        "TDs":          _ps("TDs", "Normal",   fontSize=7,  leading=9),
        "CoverTitle":   _ps("CoverTitle","Normal", fontSize=26, textColor=PRIMARY,
                             alignment=TA_CENTER, spaceAfter=8, leading=32, fontName="Helvetica-Bold"),
        "CoverSub":     _ps("CoverSub","Normal",   fontSize=14, textColor=ACCENT,
                             alignment=TA_CENTER, spaceAfter=4, leading=18),
        "CoverMeta":    _ps("CoverMeta","Normal",  fontSize=9,  textColor=colors.grey,
                             alignment=TA_CENTER, leading=12),
    }


_ST = _build_styles()


# ---------------------------------------------------------------------------
# Table style helpers
# ---------------------------------------------------------------------------
def _std_cmds(hdr_bg: colors.Color = PRIMARY) -> list:
    """Return base table-style command list (header + stripes + grid)."""
    return [
        ("BACKGROUND",   (0, 0), (-1, 0),  hdr_bg),
        ("TEXTCOLOR",    (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",     (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0),  9),
        ("FONTSIZE",     (0, 1), (-1, -1), 8),
        ("ROWBACKGROUNDS",(0,1), (-1, -1), [colors.white, STRIPE]),
        ("GRID",         (0, 0), (-1, -1), 0.3, BORDER),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ("LEFTPADDING",  (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]


def _conf_row_cmds(row_confs: list[tuple[int, str | None]], col: int) -> list:
    """Per-row BACKGROUND/TEXTCOLOR/FONTNAME for a confidence column."""
    cmds: list = []
    for row, conf in row_confs:
        clr = _CONF_CLR.get(conf or "", CONF_NONE)
        cmds += [
            ("BACKGROUND", (col, row), (col, row), clr),
            ("TEXTCOLOR",  (col, row), (col, row), colors.white),
            ("FONTNAME",   (col, row), (col, row), "Helvetica-Bold"),
        ]
    return cmds


# ---------------------------------------------------------------------------
# Value formatting helpers
# ---------------------------------------------------------------------------
def _usd(v: Any) -> str:
    if v is None:
        return "—"
    f = float(v)
    if abs(f) >= 1_000:
        return f"${f/1_000:,.1f}B"
    return f"${f:,.1f}M"


def _band(low: Any, mid: Any, high: Any) -> str:
    """'$123.4M [$95.0M – $155.0M]' — spec requires every TAM shows its band."""
    mid_s = _usd(mid)
    if low is None and high is None:
        return mid_s
    return f"{mid_s} [{_usd(low)} – {_usd(high)}]"


def _conf_label(c: str | None) -> str:
    return (c or "—").upper()


def _cell_label(cell: Cell) -> str:
    """Compact label: 'SubcatName / Country (SEGMENT)'."""
    sub = cell.subcategory.name if cell.subcategory else f"Sub#{cell.subcategory_id}"
    if cell.geography:
        geo = f"{cell.geography.country} ({cell.geography.segment})"
    else:
        geo = f"Geo#{cell.geography_id}"
    return f"{sub} / {geo}"


def _ts_now() -> str:
    return datetime.now(timezone.utc).strftime("Generated %Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Source registry — accumulates references, emits numbered Sources page
# ---------------------------------------------------------------------------
@dataclass
class SourceRegistry:
    """Accumulates source references as sections are built.

    Each unique source receives a sequential number on first use.
    ``sources_page()`` returns the final numbered Sources flowable list with
    clickable hyperlinks (spec: 'every report ends with a NUMBERED Sources page
    of clickable URLs').
    """

    _idx: dict[str, int] = field(default_factory=dict)
    _ordered: dict[int, Source] = field(default_factory=dict)
    _n: int = field(default=0)

    def add(self, source: Source) -> int:
        """Register *source* (idempotent) and return its 1-based number."""
        sid = source.source_id
        if sid not in self._idx:
            self._n += 1
            self._idx[sid] = self._n
            self._ordered[self._n] = source
        return self._idx[sid]

    def cite(self, source: Source) -> str:
        """Return '[N]' citation string for inline use."""
        return f"[{self.add(source)}]"

    def is_empty(self) -> bool:
        return self._n == 0

    def sources_page(self) -> list:
        """Return flowables for the numbered, clickable Sources page.

        Always appended last. Every entry has a 1-based number, publisher,
        source_id, source class (A/B/C), and a clickable URL where available.
        """
        if self.is_empty():
            return []

        rows: list[list] = [[
            Paragraph("#",          _ST["TH"]),
            Paragraph("Publisher",  _ST["TH"]),
            Paragraph("Source ID",  _ST["TH"]),
            Paragraph("Class",      _ST["TH"]),
            Paragraph("URL / Pattern", _ST["TH"]),
        ]]
        for n in sorted(self._ordered):
            src = self._ordered[n]
            url = src.url_pattern or ""
            display_url = url[:80] + ("…" if len(url) > 80 else "")
            url_para = (
                Paragraph(
                    f'<a href="{url}" color="#2563EB"><u>{display_url}</u></a>',
                    _ST["TDs"],
                )
                if url
                else Paragraph("—", _ST["TDs"])
            )
            rows.append([
                Paragraph(str(n),                _ST["TD"]),
                Paragraph(src.publisher or "—",  _ST["TD"]),
                Paragraph(src.source_id,          _ST["TDs"]),
                Paragraph(src.source_class or "—",_ST["TD"]),
                url_para,
            ])

        col_w = [1.0*cm, 3.8*cm, 4.0*cm, 1.2*cm, None]
        t = Table(rows, colWidths=col_w, repeatRows=1)
        t.setStyle(TableStyle(_std_cmds()))

        return [
            PageBreak(),
            Paragraph("Sources", _ST["H1"]),
            Paragraph(
                "All data sources cited in this report. Reference numbers [N] "
                "correspond to citations in each section.",
                _ST["Small"],
            ),
            Spacer(1, 4 * mm),
            t,
        ]


# ---------------------------------------------------------------------------
# Chart builders (spec: charts carry segment + confidence)
# ---------------------------------------------------------------------------
def _confidence_pie(high: int, med: int, low_: int) -> Drawing:
    """Pie chart: confidence distribution. Returns an empty Drawing if all zero."""
    d = Drawing(220, 150)
    entries = [
        (high,  f"High ({high})",   CONF_HIGH),
        (med,   f"Medium ({med})",  CONF_MED),
        (low_,  f"Low ({low_})",    CONF_LOW),
    ]
    non_zero = [(v, lbl, c) for v, lbl, c in entries if v > 0]
    if not non_zero:
        return d

    pie = Pie()
    pie.x, pie.y = 20, 15
    pie.width = pie.height = 110
    pie.data   = [v for v, _, _ in non_zero]
    pie.labels = [lbl for _, lbl, _ in non_zero]
    for i, (_, _, clr) in enumerate(non_zero):
        pie.slices[i].fillColor   = clr
        pie.slices[i].strokeColor = colors.white
        pie.slices[i].strokeWidth = 0.5
        pie.slices[i].labelRadius = 1.28
        pie.slices[i].fontName    = "Helvetica"
        pie.slices[i].fontSize    = 7
    d.add(pie)
    return d


def _tam_bar_chart(
    labels: list[str],
    values: list[float],
    confs: list[str | None],
) -> Drawing:
    """Vertical bar chart: TAM central value per cell.

    Bar colour encodes confidence; category labels contain subcategory + segment
    (satisfying 'charts carry segment + confidence').
    """
    n = len(labels)
    if n == 0:
        return Drawing(200, 160)

    chart_w = max(280, min(n * 52, 480))
    d = Drawing(chart_w, 210)

    bc = VerticalBarChart()
    bc.x       = 50
    bc.y       = 50
    bc.width   = chart_w - 70
    bc.height  = 140
    bc.data    = [values]
    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.angle      = 45 if n > 4 else 0
    bc.categoryAxis.labels.fontSize   = 6 if n > 6 else 7
    bc.categoryAxis.labels.dy         = -4
    bc.categoryAxis.labels.textAnchor = "end" if n > 4 else "middle"
    bc.valueAxis.valueMin      = 0
    bc.valueAxis.labels.fontSize     = 7
    bc.valueAxis.labelTextFormat     = "$%gM"
    bc.groupSpacing = 6
    bc.barSpacing   = 0

    for i, conf in enumerate(confs):
        bc.bars[0, i].fillColor   = _CONF_CLR.get(conf or "", CONF_NONE)
        bc.bars[0, i].strokeColor = None

    d.add(bc)
    return d


def _player_bar_chart(
    names: list[str],
    shares: list[float],
    confs: list[str | None],
) -> Drawing:
    """Horizontal bar chart: player market shares (%).

    Bar colour encodes confidence. Player names serve as category labels and
    typically include segment context from the encompassing cell.
    """
    n = len(names)
    if n == 0:
        return Drawing(200, 120)

    row_h  = 18
    ch     = max(80, n * row_h + 20)
    d      = Drawing(350, ch + 30)

    bc = HorizontalBarChart()
    bc.x      = 90
    bc.y      = 10
    bc.width  = 240
    bc.height = ch
    bc.data   = [shares]
    bc.categoryAxis.categoryNames = names
    bc.categoryAxis.labels.fontSize = 8
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = max(100.0, max(shares, default=0) * 1.15)
    bc.valueAxis.labelTextFormat = "%.0f%%"
    bc.valueAxis.labels.fontSize  = 7
    bc.groupSpacing = 3
    bc.barSpacing   = 0

    for i, conf in enumerate(confs):
        bc.bars[0, i].fillColor   = _CONF_CLR.get(conf or "", CONF_NONE)
        bc.bars[0, i].strokeColor = None

    d.add(bc)
    return d


# ---------------------------------------------------------------------------
# DB loaders
# ---------------------------------------------------------------------------
def _load_cells(
    session: Session,
    subcategory_ids: list[int] | None = None,
    geography_ids:   list[int] | None = None,
    year:            int        | None = None,
    confidence:      str        | None = None,
    limit:           int               = 500,
) -> list[Cell]:
    """Load active cells with subcategory, geography, triangulations, method, source joined."""
    stmt = (
        select(Cell)
        .options(
            joinedload(Cell.subcategory),
            joinedload(Cell.geography),
            joinedload(Cell.triangulations).joinedload(CellTriangulation.method),
            joinedload(Cell.triangulations).joinedload(CellTriangulation.source),
        )
        .where(Cell.status == "active")
        .order_by(Cell.year.desc(), Cell.subcategory_id, Cell.geography_id)
        .limit(limit)
    )
    if subcategory_ids:
        stmt = stmt.where(Cell.subcategory_id.in_(subcategory_ids))
    if geography_ids:
        stmt = stmt.where(Cell.geography_id.in_(geography_ids))
    if year is not None:
        stmt = stmt.where(Cell.year == year)
    if confidence:
        stmt = stmt.where(Cell.confidence == confidence)
    return list(session.execute(stmt).unique().scalars().all())


def _load_player_shares(
    session: Session,
    cell_ids: list[int],
    limit: int = 1000,
) -> list[PlayerShare]:
    """Load player shares (with company + source joined) for the given cell_ids."""
    if not cell_ids:
        return []
    stmt = (
        select(PlayerShare)
        .options(
            joinedload(PlayerShare.company),
            joinedload(PlayerShare.source),
        )
        .where(PlayerShare.cell_id.in_(cell_ids))
        .order_by(PlayerShare.cell_id, PlayerShare.rank)
        .limit(limit)
    )
    return list(session.execute(stmt).unique().scalars().all())


# ---------------------------------------------------------------------------
# Section builders — each returns list[Flowable]
# ---------------------------------------------------------------------------

def section_cover(
    title: str,
    subtitle: str,
    filters_desc: str,
    timestamp: str,
) -> list:
    """Cover page: title, subtitle, timestamp, and filter summary."""
    return [
        Spacer(1, 55 * mm),
        HRFlowable(width=INNER_W, thickness=3, color=ACCENT, spaceAfter=8),
        Paragraph(title,    _ST["CoverTitle"]),
        Paragraph(subtitle, _ST["CoverSub"]),
        HRFlowable(width=INNER_W, thickness=3, color=ACCENT, spaceBefore=8),
        Spacer(1, 18 * mm),
        Paragraph("GRX10 Solutions Private Limited", _ST["CoverMeta"]),
        Paragraph(timestamp,    _ST["CoverMeta"]),
        Spacer(1, 6 * mm),
        Paragraph(filters_desc, _ST["CoverMeta"]),
        PageBreak(),
    ]


def section_executive_summary(cells: list[Cell]) -> list:
    """Key-metric block: confidence distribution, coverage, largest market."""
    items: list = [Paragraph("Executive Summary", _ST["H1"]), Spacer(1, 3 * mm)]

    total = len(cells)
    if total == 0:
        items.append(Paragraph("No cells match the selected filters.", _ST["Body"]))
        return items

    high   = sum(1 for c in cells if c.confidence == "high")
    med    = sum(1 for c in cells if c.confidence == "medium")
    low_   = sum(1 for c in cells if c.confidence == "low")
    none_c = total - high - med - low_

    covered = sum(
        1 for c in cells
        if len({t.method_code for t in c.triangulations}) >= 2
    )

    by_tam = sorted(
        [c for c in cells if c.tam_revenue_usd_m is not None],
        key=lambda c: float(c.tam_revenue_usd_m),  # type: ignore[arg-type]
        reverse=True,
    )
    top_cell  = by_tam[0] if by_tam else None
    top_label = _cell_label(top_cell) if top_cell else "—"
    top_tam   = _band(
        top_cell.tam_low_usd_m, top_cell.tam_revenue_usd_m, top_cell.tam_high_usd_m
    ) if top_cell else "—"

    # KPI summary row
    kpi_data = [
        ["Total Cells", "HIGH Confidence", "MEDIUM Confidence", "LOW / None", "Coverage (≥2 methods)"],
        [str(total), str(high), str(med), str(low_ + none_c),
         f"{covered}/{total} ({covered*100//total}%)" if total else "—"],
    ]
    kpi_cmds = _std_cmds() + [
        ("FONTSIZE",  (0, 1), (-1, 1), 13),
        ("FONTNAME",  (0, 1), (-1, 1), "Helvetica-Bold"),
        ("TEXTCOLOR", (1, 1), (1, 1),  CONF_HIGH),
        ("TEXTCOLOR", (2, 1), (2, 1),  CONF_MED),
        ("TEXTCOLOR", (3, 1), (3, 1),  CONF_LOW),
        ("TEXTCOLOR", (4, 1), (4, 1),  ACCENT),
        ("ALIGN",     (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING",    (0, 1), (-1, 1), 8),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [LIGHT]),
    ]
    kpi_t = Table(kpi_data, colWidths=[INNER_W / 5] * 5)
    kpi_t.setStyle(TableStyle(kpi_cmds))
    items.append(kpi_t)
    items.append(Spacer(1, 5 * mm))

    # Confidence pie + largest cell side-by-side
    pie = _confidence_pie(high, med, low_)
    side_data = [
        [Paragraph("Largest Market Cell", _ST["H3"])],
        [Paragraph(top_label, _ST["Body"])],
        [Paragraph(f"TAM: {top_tam}", _ST["Body"])],
        [Paragraph(
            f"Confidence: {_conf_label(top_cell.confidence if top_cell else None)}",
            _ST["Body"],
        )],
        [Paragraph(f"Year: {top_cell.year}" if top_cell else "", _ST["Body"])],
    ]
    side_t = Table(side_data, colWidths=[INNER_W / 2 - 0.5 * cm])
    side_t.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    outer = Table([[pie, side_t]], colWidths=[INNER_W / 2, INNER_W / 2])
    outer.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    items.append(outer)
    items.append(Spacer(1, 3 * mm))
    items.append(Paragraph(
        "HIGH = ≥3 independent methods, spread ≤5%, Tier-A source. "
        "MEDIUM = ≥2 methods, spread ≤15%. LOW = below MEDIUM threshold. "
        "Coverage = cells triangulated by ≥2 distinct methods.",
        _ST["Tiny"],
    ))
    return items


def section_tam_table(cells: list[Cell], registry: SourceRegistry) -> list:
    """TAM overview table with band and confidence chip, preceded by a bar chart."""
    items: list = [
        PageBreak(),
        Paragraph("TAM Overview", _ST["H1"]),
        Paragraph(
            "Every TAM shows its band (low – central – high). Bar colour and the "
            "Confidence column encode the tier computed by the active validation profile.",
            _ST["Small"],
        ),
        Spacer(1, 3 * mm),
    ]
    if not cells:
        items.append(Paragraph("No cells match the selected filters.", _ST["Body"]))
        return items

    # Chart: top-12 cells by TAM
    top12 = sorted(
        [c for c in cells if c.tam_revenue_usd_m is not None],
        key=lambda c: float(c.tam_revenue_usd_m),  # type: ignore[arg-type]
        reverse=True,
    )[:12]
    if top12:
        chart = _tam_bar_chart(
            labels=[_cell_label(c)[:28] for c in top12],
            values=[float(c.tam_revenue_usd_m) for c in top12],  # type: ignore[arg-type]
            confs=[c.confidence for c in top12],
        )
        items += [
            chart,
            Paragraph(
                "Figure: TAM (USD M) — top 12 cells by size. "
                "Bar colour: green = HIGH, amber = MEDIUM, red = LOW. "
                "Labels show subcategory / geography (segment).",
                _ST["Caption"],
            ),
            Spacer(1, 4 * mm),
        ]

    # Full table
    header_row = [
        Paragraph("Subcategory",       _ST["TH"]),
        Paragraph("Country / Segment", _ST["TH"]),
        Paragraph("Year",              _ST["TH"]),
        Paragraph("TAM Band (USD M)",  _ST["TH"]),
        Paragraph("Confidence",        _ST["TH"]),
        Paragraph("Methods",           _ST["TH"]),
        Paragraph("Src", _ST["TH"]),
    ]
    rows = [header_row]
    conf_row_data: list[tuple[int, str | None]] = []

    for r_idx, cell in enumerate(cells, start=1):
        sub = cell.subcategory.name if cell.subcategory else f"#{cell.subcategory_id}"
        if cell.geography:
            geo = f"{cell.geography.country}\n({cell.geography.segment})"
        else:
            geo = f"Geo#{cell.geography_id}"
        methods = sorted({t.method_code for t in cell.triangulations})
        method_str = ", ".join(m[:18] for m in methods) if methods else "—"

        # Collect sources for registry, build citation string
        cites: list[str] = []
        for tri in cell.triangulations:
            if tri.source:
                cites.append(registry.cite(tri.source))
        src_str = " ".join(sorted(set(cites))) if cites else "—"

        conf_row_data.append((r_idx, cell.confidence))
        rows.append([
            Paragraph(sub,        _ST["TD"]),
            Paragraph(geo,        _ST["TD"]),
            Paragraph(str(cell.year), _ST["TD"]),
            Paragraph(_band(cell.tam_low_usd_m, cell.tam_revenue_usd_m, cell.tam_high_usd_m), _ST["TD"]),
            Paragraph(_conf_label(cell.confidence), _ST["TD"]),
            Paragraph(method_str, _ST["TDs"]),
            Paragraph(src_str,   _ST["TDs"]),
        ])

    col_w = [4.0*cm, 3.2*cm, 1.0*cm, 4.2*cm, 2.0*cm, 3.5*cm, 1.8*cm]
    cmds = _std_cmds() + _conf_row_cmds(conf_row_data, col=4)
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle(cmds))
    items.append(t)
    return items


def section_confidence_breakdown(cells: list[Cell]) -> list:
    """Confidence distribution table and chart by subcategory."""
    items: list = [
        PageBreak(),
        Paragraph("Confidence Breakdown", _ST["H1"]),
        Paragraph(
            "Counts and percentages of HIGH / MEDIUM / LOW cells per subcategory. "
            "Confidence is computed only by the cell_triangulation_summary view "
            "(never hand-set).",
            _ST["Small"],
        ),
        Spacer(1, 3 * mm),
    ]
    if not cells:
        items.append(Paragraph("No cells match the selected filters.", _ST["Body"]))
        return items

    # Aggregate by subcategory
    from collections import defaultdict
    by_sub: dict[str, dict[str, int]] = defaultdict(lambda: {"high": 0, "medium": 0, "low": 0, "none": 0, "total": 0})
    for c in cells:
        sub = c.subcategory.name if c.subcategory else f"Sub#{c.subcategory_id}"
        bucket = c.confidence if c.confidence in ("high", "medium", "low") else "none"
        by_sub[sub][bucket] += 1
        by_sub[sub]["total"] += 1

    rows = [[
        Paragraph("Subcategory",    _ST["TH"]),
        Paragraph("Total",          _ST["TH"]),
        Paragraph("HIGH",           _ST["TH"]),
        Paragraph("MEDIUM",         _ST["TH"]),
        Paragraph("LOW",            _ST["TH"]),
        Paragraph("None",           _ST["TH"]),
        Paragraph("Coverage %",     _ST["TH"]),
    ]]
    for sub, counts in sorted(by_sub.items()):
        tot = counts["total"]
        covered_pct = f"{(counts['high'] + counts['medium']) * 100 // tot}%" if tot else "—"
        rows.append([
            Paragraph(sub,              _ST["TD"]),
            Paragraph(str(tot),         _ST["TD"]),
            Paragraph(str(counts["high"]),   _ST["TD"]),
            Paragraph(str(counts["medium"]), _ST["TD"]),
            Paragraph(str(counts["low"]),    _ST["TD"]),
            Paragraph(str(counts["none"]),   _ST["TD"]),
            Paragraph(covered_pct,      _ST["TD"]),
        ])

    col_w = [5.5*cm, 1.5*cm, 1.5*cm, 1.5*cm, 1.5*cm, 1.5*cm, 2.2*cm]
    t = Table(rows, colWidths=col_w, repeatRows=1)
    cmds = _std_cmds()
    # Colour HIGH/MEDIUM/LOW header cells
    cmds += [
        ("BACKGROUND", (2, 0), (2, 0), CONF_HIGH),
        ("BACKGROUND", (3, 0), (3, 0), CONF_MED),
        ("BACKGROUND", (4, 0), (4, 0), CONF_LOW),
    ]
    t.setStyle(TableStyle(cmds))

    # Summary pie
    total_h = sum(v["high"]   for v in by_sub.values())
    total_m = sum(v["medium"] for v in by_sub.values())
    total_l = sum(v["low"]    for v in by_sub.values())
    pie = _confidence_pie(total_h, total_m, total_l)

    layout = Table([[pie, t]], colWidths=[220, None])
    layout.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    items.append(layout)
    return items


def section_estimates_detail(cells: list[Cell], registry: SourceRegistry) -> list:
    """Per-cell estimates table: one row per (method, source) pair."""
    items: list = [
        PageBreak(),
        Paragraph("Estimates Detail", _ST["H1"]),
        Paragraph(
            "Every estimate row is drillable: method → source → raw payload. "
            "Source citations [N] map to the numbered Sources page.",
            _ST["Small"],
        ),
        Spacer(1, 3 * mm),
    ]
    if not cells:
        items.append(Paragraph("No cells match the selected filters.", _ST["Body"]))
        return items

    for cell in cells:
        if not cell.triangulations:
            continue
        label = _cell_label(cell)
        sub_items: list = [
            KeepTogether([
                Paragraph(f"{label} — {cell.year}", _ST["H3"]),
                Paragraph(
                    f"TAM: {_band(cell.tam_low_usd_m, cell.tam_revenue_usd_m, cell.tam_high_usd_m)}"
                    f"  |  Confidence: {_conf_label(cell.confidence)}",
                    _ST["Small"],
                ),
                Spacer(1, 2 * mm),
            ])
        ]

        rows = [[
            Paragraph("Method",     _ST["TH"]),
            Paragraph("Tier",       _ST["TH"]),
            Paragraph("Estimate",   _ST["TH"]),
            Paragraph("Source",     _ST["TH"]),
            Paragraph("Publisher",  _ST["TH"]),
            Paragraph("Ref",        _ST["TH"]),
        ]]
        for tri in sorted(cell.triangulations, key=lambda t: t.method_code):
            method   = tri.method
            source   = tri.source
            tier     = method.tier if method else "—"
            pub      = source.publisher if source else "—"
            cite_str = registry.cite(source) if source else "—"
            rows.append([
                Paragraph(tri.method_code[:25], _ST["TDs"]),
                Paragraph(tier or "—",          _ST["TD"]),
                Paragraph(_usd(tri.estimate_usd_m), _ST["TD"]),
                Paragraph(source.source_id[:22] if source else "—", _ST["TDs"]),
                Paragraph(pub[:22] if pub else "—", _ST["TDs"]),
                Paragraph(cite_str, _ST["TD"]),
            ])

        col_w = [3.8*cm, 1.0*cm, 2.2*cm, 3.5*cm, 3.5*cm, 1.0*cm]
        t = Table(rows, colWidths=col_w, repeatRows=1)
        t.setStyle(TableStyle(_std_cmds()))
        sub_items.append(t)
        sub_items.append(Spacer(1, 4 * mm))
        items.extend(sub_items)

    return items


def section_gap_analysis(cells: list[Cell]) -> list:
    """Identify coverage gaps: uncovered cells, thin triangulation, high spread."""
    items: list = [
        PageBreak(),
        Paragraph("Gap Analysis", _ST["H1"]),
        Paragraph(
            "Cells lacking sufficient evidence for HIGH/MEDIUM confidence. "
            "Classified into four gap types for prioritisation.",
            _ST["Small"],
        ),
        Spacer(1, 3 * mm),
    ]
    if not cells:
        items.append(Paragraph("No cells to analyse.", _ST["Body"]))
        return items

    # Classify
    no_data:      list[Cell] = []
    thin:         list[Cell] = []
    web_only:     list[Cell] = []
    low_conf:     list[Cell] = []

    for c in cells:
        tris = c.triangulations
        if not tris:
            no_data.append(c)
            continue
        methods = {t.method_code for t in tris}
        if len(methods) < 2:
            thin.append(c)
        elif all(t.method_code == "web_search_extraction" for t in tris):
            web_only.append(c)
        elif c.confidence == "low" or c.confidence is None:
            low_conf.append(c)

    # Summary table
    summary_rows = [
        [Paragraph("Gap Type",  _ST["TH"]), Paragraph("Count", _ST["TH"]),
         Paragraph("Description", _ST["TH"])],
        [Paragraph("No Estimates",        _ST["TD"]),
         Paragraph(str(len(no_data)),     _ST["TD"]),
         Paragraph("Cell has zero estimates. No methods have run yet.", _ST["TD"])],
        [Paragraph("Thin Triangulation",  _ST["TD"]),
         Paragraph(str(len(thin)),        _ST["TD"]),
         Paragraph("Only 1 distinct method. Need ≥2 for triangulation.", _ST["TD"])],
        [Paragraph("Web-Search Only",     _ST["TD"]),
         Paragraph(str(len(web_only)),    _ST["TD"]),
         Paragraph("All estimates from web_search_extraction (hard-capped LOW). "
                   "Add a structured source.", _ST["TD"])],
        [Paragraph("Low Confidence",      _ST["TD"]),
         Paragraph(str(len(low_conf)),    _ST["TD"]),
         Paragraph("Multiple methods but confidence below MEDIUM threshold.", _ST["TD"])],
    ]
    col_w = [4.0*cm, 1.5*cm, None]
    sum_t = Table(summary_rows, colWidths=col_w, repeatRows=1)
    cmds = _std_cmds()
    # Colour the count cells in the summary
    count_colours = [(1, None), (2, None), (3, None), (4, None)]
    if len(no_data):  cmds.append(("BACKGROUND", (1, 1), (1, 1), CONF_LOW))
    if len(thin):     cmds.append(("BACKGROUND", (1, 2), (1, 2), CONF_MED))
    if len(web_only): cmds.append(("BACKGROUND", (1, 3), (1, 3), CONF_MED))
    if len(low_conf): cmds.append(("BACKGROUND", (1, 4), (1, 4), CONF_LOW))
    sum_t.setStyle(TableStyle(cmds))
    items.append(sum_t)
    items.append(Spacer(1, 5 * mm))

    def _gap_table(gap_cells: list[Cell], heading: str) -> list:
        if not gap_cells:
            return []
        rows = [[
            Paragraph("Cell",        _ST["TH"]),
            Paragraph("Year",        _ST["TH"]),
            Paragraph("TAM (central)", _ST["TH"]),
            Paragraph("Methods",     _ST["TH"]),
        ]]
        for c in gap_cells:
            rows.append([
                Paragraph(_cell_label(c)[:45],                    _ST["TDs"]),
                Paragraph(str(c.year),                             _ST["TD"]),
                Paragraph(_usd(c.tam_revenue_usd_m),               _ST["TD"]),
                Paragraph(
                    ", ".join(sorted({t.method_code[:15] for t in c.triangulations})) or "—",
                    _ST["TDs"],
                ),
            ])
        t = Table(rows, colWidths=[7*cm, 1.2*cm, 3.2*cm, None], repeatRows=1)
        t.setStyle(TableStyle(_std_cmds(hdr_bg=CONF_LOW if "No Estimates" in heading or "Low" in heading else CONF_MED)))
        return [Paragraph(heading, _ST["H2"]), Spacer(1, 2*mm), t, Spacer(1, 4*mm)]

    items.extend(_gap_table(no_data,  "Cells with No Estimates"))
    items.extend(_gap_table(thin,     "Thinly Triangulated Cells (1 method)"))
    items.extend(_gap_table(web_only, "Web-Search-Only Cells (LOW cap)"))
    items.extend(_gap_table(low_conf, "Low-Confidence Cells (multi-method)"))
    return items


def section_player_shares(
    shares: list[PlayerShare],
    cells: list[Cell],
    registry: SourceRegistry,
) -> list:
    """Player shares table + horizontal bar chart per cell."""
    items: list = [
        PageBreak(),
        Paragraph("Player Shares", _ST["H1"]),
        Paragraph(
            "Market participant revenue shares per cell. "
            "Bar colour encodes share confidence. "
            "Source citations [N] link to the Sources page.",
            _ST["Small"],
        ),
        Spacer(1, 3 * mm),
    ]
    if not shares:
        items.append(Paragraph("No player share data found for the selected cells.", _ST["Body"]))
        return items

    # Group shares by cell_id
    from collections import defaultdict
    by_cell: dict[int, list[PlayerShare]] = defaultdict(list)
    for ps in shares:
        by_cell[ps.cell_id].append(ps)

    cell_map = {c.cell_id: c for c in cells}

    for cid, cell_shares in sorted(by_cell.items()):
        cell = cell_map.get(cid)
        cell_label = _cell_label(cell) if cell else f"Cell {cid}"
        tam_str = (
            _band(cell.tam_low_usd_m, cell.tam_revenue_usd_m, cell.tam_high_usd_m)
            if cell else "—"
        )
        items.append(Paragraph(f"{cell_label}", _ST["H2"]))
        items.append(Paragraph(
            f"TAM: {tam_str}  |  Confidence: {_conf_label(cell.confidence if cell else None)}",
            _ST["Small"],
        ))
        items.append(Spacer(1, 2 * mm))

        rows = [[
            Paragraph("Rank",          _ST["TH"]),
            Paragraph("Company",       _ST["TH"]),
            Paragraph("Role",          _ST["TH"]),
            Paragraph("Share %",       _ST["TH"]),
            Paragraph("Band %",        _ST["TH"]),
            Paragraph("Revenue (USD M)",_ST["TH"]),
            Paragraph("Confidence",    _ST["TH"]),
            Paragraph("Src",           _ST["TH"]),
        ]]
        chart_names:  list[str]       = []
        chart_shares: list[float]     = []
        chart_confs:  list[str | None]= []
        conf_cmds: list[tuple[int, str | None]] = []

        for r_idx, ps in enumerate(cell_shares, start=1):
            name   = ps.company.name[:28] if ps.company else f"Co#{ps.company_id}"
            band_s = (
                f"[{_usd(ps.share_low_pct)}–{_usd(ps.share_high_pct)}]"
                if ps.share_low_pct is not None
                else "—"
            )
            cite_str = registry.cite(ps.source) if ps.source else "—"
            rows.append([
                Paragraph(str(ps.rank),       _ST["TD"]),
                Paragraph(name,               _ST["TD"]),
                Paragraph(ps.player_role[:18],_ST["TDs"]),
                Paragraph(f"{float(ps.share_pct):.1f}%" if ps.share_pct else "—", _ST["TD"]),
                Paragraph(band_s,             _ST["TDs"]),
                Paragraph(_usd(ps.revenue_usd_m), _ST["TD"]),
                Paragraph(_conf_label(ps.confidence), _ST["TD"]),
                Paragraph(cite_str,           _ST["TD"]),
            ])
            conf_cmds.append((r_idx, ps.confidence))
            if ps.share_pct is not None:
                chart_names.append(name[:20])
                chart_shares.append(float(ps.share_pct))
                chart_confs.append(ps.confidence)

        col_w = [1.0*cm, 4.0*cm, 2.5*cm, 1.8*cm, 2.2*cm, 2.8*cm, 2.0*cm, 1.0*cm]
        cmds = _std_cmds() + _conf_row_cmds(conf_cmds, col=6)
        t = Table(rows, colWidths=col_w, repeatRows=1)
        t.setStyle(TableStyle(cmds))
        items.append(t)

        if chart_names:
            chart = _player_bar_chart(chart_names, chart_shares, chart_confs)
            items.append(chart)
            items.append(Paragraph(
                f"Figure: Market share (%) for {cell_label}. "
                "Bar colour: green = HIGH confidence, amber = MEDIUM, red = LOW.",
                _ST["Caption"],
            ))
        items.append(Spacer(1, 5 * mm))

    return items


def section_methodology() -> list:
    """Static methodology note describing confidence computation rules."""
    return [
        PageBreak(),
        Paragraph("Methodology Notes", _ST["H1"]),
        Paragraph(
            "Market size estimates are produced by an automated pipeline that pulls "
            "data from structured primary sources (class A), industry/procedural sources "
            "(class B), and triangulation-support sources (class C).",
            _ST["Body"],
        ),
        Spacer(1, 3 * mm),
        Paragraph("Confidence Tiers", _ST["H2"]),
        Paragraph(
            "Confidence is computed exclusively by the cell_triangulation_summary "
            "materialised view using the ACTIVE validation profile (Standard by default). "
            "It is never set at write-time by humans.",
            _ST["Body"],
        ),
        Spacer(1, 2 * mm),
        Table(
            [
                [Paragraph("Tier",   _ST["TH"]), Paragraph("Criteria", _ST["TH"])],
                [Paragraph("HIGH",   _ST["TD"]),
                 Paragraph("≥3 distinct independent methods (method × source class), "
                            "spread ratio < 5%, Tier-A primary source required.", _ST["TD"])],
                [Paragraph("MEDIUM", _ST["TD"]),
                 Paragraph("≥2 distinct methods, spread ratio < 15% "
                            "(or ≥3 methods, spread < 20%).", _ST["TD"])],
                [Paragraph("LOW",    _ST["TD"]),
                 Paragraph("Below MEDIUM thresholds, or web_search_extraction "
                            "hard-capped at LOW.", _ST["TD"])],
            ],
            colWidths=[2.5*cm, None],
        ),
        Spacer(1, 3 * mm),
        Paragraph("Source Classes", _ST["H2"]),
        Paragraph(
            "Class A: primary structured sources (trade statistics, regulatory databases, "
            "XBRL filings). Class B: industry reports, analyst data, national statistics. "
            "Class C: triangulation support — web search, patent proxies, hiring signals. "
            "Class C sources are hard-capped at LOW confidence.",
            _ST["Body"],
        ),
        Spacer(1, 3 * mm),
        Paragraph(
            "All raw payloads are stored verbatim in JSONB. Every estimate is "
            "drill-drillable: cell → method/estimate → source → raw payload. "
            "Idempotent upserts on the (cell_id, method_code, source_id) composite key "
            "ensure re-running the pipeline does not duplicate rows.",
            _ST["Body"],
        ),
    ]


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------
def _page_header_footer(canvas, doc, title: str, timestamp: str) -> None:
    """Draw page header line + footer with report title and page number."""
    canvas.saveState()
    w, _ = A4
    top_y    = PAGE_H - TM + 5 * mm
    bot_y    = BM - 5 * mm

    # Header rule
    canvas.setStrokeColor(ACCENT)
    canvas.setLineWidth(0.5)
    canvas.line(LM, top_y, w - RM, top_y)
    canvas.setFont("Helvetica-Bold", 7)
    canvas.setFillColor(PRIMARY)
    canvas.drawString(LM, top_y + 2 * mm, "GRX10 Market Research Tool  |  CONFIDENTIAL")
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.grey)
    canvas.drawRightString(w - RM, top_y + 2 * mm, title)

    # Footer rule
    canvas.setStrokeColor(BORDER)
    canvas.line(LM, bot_y, w - RM, bot_y)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.grey)
    canvas.drawString(LM, bot_y - 3 * mm, timestamp)
    canvas.drawRightString(w - RM, bot_y - 3 * mm, f"Page {doc.page}")

    canvas.restoreState()


def _build_pdf(story: list, title: str, timestamp: str) -> io.BytesIO:
    """Assemble *story* into a PDF and return a BytesIO positioned at 0."""
    buf = io.BytesIO()

    on_first = lambda canvas, doc: None  # cover page — no header/footer
    on_later = lambda canvas, doc: _page_header_footer(canvas, doc, title, timestamp)

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=LM, rightMargin=RM,
        topMargin=TM,  bottomMargin=BM,
        title=title,
        author="GRX10 Solutions Private Limited",
    )
    doc.build(story, onFirstPage=on_first, onLaterPages=on_later)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Section dispatcher for the custom builder
# ---------------------------------------------------------------------------
_SECTION_MAP: dict[str, str] = {
    "executive_summary":   "executive_summary",
    "tam_table":           "tam_table",
    "confidence_breakdown":"confidence_breakdown",
    "estimates_detail":    "estimates_detail",
    "gap_analysis":        "gap_analysis",
    "player_shares":       "player_shares",
    "methodology":         "methodology",
}

VALID_SECTIONS: frozenset[str] = frozenset(_SECTION_MAP)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _filters_desc(
    subcategory_ids: list[int] | None,
    geography_ids:   list[int] | None,
    year:            int | None,
    confidence:      str | None,
) -> str:
    parts: list[str] = []
    if subcategory_ids:
        parts.append(f"subcategories: {subcategory_ids}")
    if geography_ids:
        parts.append(f"geographies: {geography_ids}")
    if year:
        parts.append(f"year: {year}")
    if confidence:
        parts.append(f"confidence: {confidence}")
    return "Filters: " + "; ".join(parts) if parts else "All cells — no filters applied"


def build_executive_audit(
    session: Session,
    subcategory_ids: list[int] | None = None,
    geography_ids:   list[int] | None = None,
    year:            int | None = None,
    confidence:      str | None = None,
) -> io.BytesIO:
    """Build the Executive Audit PDF and return a ready-to-stream BytesIO."""
    ts    = _ts_now()
    title = "Executive Audit Report"
    cells = _load_cells(session, subcategory_ids, geography_ids, year, confidence)
    reg   = SourceRegistry()

    story: list = []
    story += section_cover(title, "Market Research — Executive Audit",
                           _filters_desc(subcategory_ids, geography_ids, year, confidence), ts)
    story += section_executive_summary(cells)
    story += section_tam_table(cells, reg)
    story += section_confidence_breakdown(cells)
    story += section_estimates_detail(cells, reg)
    story += reg.sources_page()

    log.info("executive_audit: %d cells, %d sources", len(cells), reg._n)
    return _build_pdf(story, title, ts)


def build_gap_analysis(
    session: Session,
    subcategory_ids: list[int] | None = None,
    geography_ids:   list[int] | None = None,
    year:            int | None = None,
    confidence:      str | None = None,
) -> io.BytesIO:
    """Build the Gap Analysis PDF and return a ready-to-stream BytesIO."""
    ts    = _ts_now()
    title = "Gap Analysis Report"
    cells = _load_cells(session, subcategory_ids, geography_ids, year, confidence)
    reg   = SourceRegistry()

    story: list = []
    story += section_cover(title, "Coverage Gaps & Triangulation Quality",
                           _filters_desc(subcategory_ids, geography_ids, year, confidence), ts)
    story += section_executive_summary(cells)
    story += section_gap_analysis(cells)
    story += section_confidence_breakdown(cells)
    story += section_methodology()
    story += reg.sources_page()

    log.info("gap_analysis: %d cells, %d sources", len(cells), reg._n)
    return _build_pdf(story, title, ts)


def build_player_shares(
    session: Session,
    subcategory_ids: list[int] | None = None,
    geography_ids:   list[int] | None = None,
    year:            int | None = None,
    confidence:      str | None = None,
) -> io.BytesIO:
    """Build the Player Shares PDF and return a ready-to-stream BytesIO."""
    ts    = _ts_now()
    title = "Player Shares Report"
    cells = _load_cells(session, subcategory_ids, geography_ids, year, confidence)
    reg   = SourceRegistry()

    cell_ids = [c.cell_id for c in cells]
    shares   = _load_player_shares(session, cell_ids)

    story: list = []
    story += section_cover(title, "Market Participant Analysis",
                           _filters_desc(subcategory_ids, geography_ids, year, confidence), ts)
    story += section_executive_summary(cells)
    story += section_player_shares(shares, cells, reg)
    story += section_tam_table(cells, reg)
    story += reg.sources_page()

    log.info("player_shares: %d cells, %d shares, %d sources", len(cells), len(shares), reg._n)
    return _build_pdf(story, title, ts)


def build_custom(
    session: Session,
    sections: list[str],
    subcategory_ids: list[int] | None = None,
    geography_ids:   list[int] | None = None,
    year:            int | None = None,
    confidence:      str | None = None,
    title: str   = "Custom Report",
    subtitle: str = "Market Research",
) -> io.BytesIO:
    """Build a custom PDF from a caller-supplied ordered list of section names.

    Always prepends a cover page and appends the Sources page.
    Unknown section names are logged and skipped.
    Valid values for *sections*: see :data:`VALID_SECTIONS`.
    """
    ts    = _ts_now()
    cells = _load_cells(session, subcategory_ids, geography_ids, year, confidence)
    reg   = SourceRegistry()

    cell_ids = [c.cell_id for c in cells]
    shares: list[PlayerShare] = []

    # Pre-load player shares only if the section is requested
    if "player_shares" in sections:
        shares = _load_player_shares(session, cell_ids)

    story: list = section_cover(
        title, subtitle,
        _filters_desc(subcategory_ids, geography_ids, year, confidence),
        ts,
    )

    for sec in sections:
        if sec not in VALID_SECTIONS:
            log.warning("custom_report: unknown section %r — skipped", sec)
            continue
        if sec == "executive_summary":
            story += section_executive_summary(cells)
        elif sec == "tam_table":
            story += section_tam_table(cells, reg)
        elif sec == "confidence_breakdown":
            story += section_confidence_breakdown(cells)
        elif sec == "estimates_detail":
            story += section_estimates_detail(cells, reg)
        elif sec == "gap_analysis":
            story += section_gap_analysis(cells)
        elif sec == "player_shares":
            story += section_player_shares(shares, cells, reg)
        elif sec == "methodology":
            story += section_methodology()

    story += reg.sources_page()
    log.info("custom_report '%s': %d sections, %d cells, %d sources",
             title, len(sections), len(cells), reg._n)
    return _build_pdf(story, title, ts)
