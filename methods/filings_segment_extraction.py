"""``filings_segment_extraction`` — map listed-company segment revenue to a cell.

Reads ``raw_filings`` (EDINET XBRL segments, SEC segment disclosures …) and
sums the reported revenue whose business **segment** matches the cell's
subcategory and/or whose disclosed **geography** matches the cell's country,
for the cell year. Each filer's segment/geographic revenue line is a direct
top-down observation of the market the cell represents.

Matching is deliberately conservative:

* A segment line counts when its ``segment`` text shares a keyword with the
  subcategory name (e.g. filing segment "Capacitors" ↔ subcategory
  "Passives (capacitors, resistors, inductors)").
* A geographic line counts when its ``geography`` text denotes the cell
  country.
* Consolidated totals (both ``segment`` and ``geography`` empty) are ignored —
  they are not attributable to a single cell.

One ``cell_triangulation`` row per contributing source; ``estimate_usd_m`` is
the summed ``revenue_usd`` (already USD-normalised by the connector) in
millions. Tier A / class A / primary source.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy.engine import Connection

from methods._common import (
    country_matches,
    fetch_rows,
    period_prefix,
    subcategory_keywords,
    text_mentions,
    usd_to_musd,
    year_of,
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.filings_segment_extraction")


@register("filings_segment_extraction")
class FilingsSegmentExtraction(Method):
    """Sum filer segment/geographic revenue attributable to the cell."""

    method_code = "filings_segment_extraction"
    required_raw_tables = ["raw_filings"]

    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        keywords = subcategory_keywords(cell)
        if not keywords:
            return []

        year = int(cell["year"])
        rows = fetch_rows(
            session,
            "SELECT source_id, filer, segment, geography, revenue_usd, period "
            "FROM raw_filings "
            "WHERE revenue_usd IS NOT NULL AND period LIKE :yp",
            {"yp": period_prefix(year)},
        )

        totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        filers: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            if year_of(r.get("period")) != year:
                continue
            segment = str(r.get("segment") or "")
            geography = str(r.get("geography") or "")
            # Segment-attributed revenue for this subcategory ...
            seg_hit = bool(segment) and text_mentions(segment, keywords)
            # ... or geography-attributed revenue for this country.
            geo_hit = bool(geography) and country_matches(geography, cell.get("country"))
            if not (seg_hit or geo_hit):
                continue
            totals[r["source_id"]] += Decimal(str(r["revenue_usd"]))
            if r.get("filer"):
                filers[r["source_id"]].add(str(r["filer"]))

        results: list[dict[str, Any]] = []
        for source_id, total_usd in totals.items():
            est = usd_to_musd(total_usd)
            if est is None or est <= 0:
                continue
            names = ", ".join(sorted(filers[source_id])[:5]) or "filers"
            results.append(self.row(
                estimate_usd_m=est,
                source_id=source_id,
                notes=(
                    f"Segment/geographic revenue for {cell.get('subcategory_name')} "
                    f"in {cell.get('country')} ({year}) from {names}"
                ),
            ))
        return results
