"""``tender_award_aggregation`` — size a cell from public procurement awards.

Sums award values in ``raw_procurement`` (USASpending, OCDS publishers) for
the cell's country and year. Public-tender spend is a floor-style observation
of addressable demand: it captures the government-procured slice of the market
the cell represents.

Procurement records carry no HS/segment dimension, so this method scopes by
country + year only. It is therefore most meaningful for DOMESTIC cells (the
government buys within its own market); IMPORT/EXPORT cells are skipped to
avoid mixing a domestic-demand signal into a trade-direction cell.

One ``cell_triangulation`` row per contributing source; ``estimate_usd_m`` is
the summed award ``value_usd`` in millions. Tier A / class A / primary source.
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
    usd_to_musd,
    year_of,
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.tender_award_aggregation")


@register("tender_award_aggregation")
class TenderAwardAggregation(Method):
    """Aggregate public procurement award values for the cell's country/year."""

    method_code = "tender_award_aggregation"
    required_raw_tables = ["raw_procurement"]

    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        segment = str(cell.get("segment") or "").upper()
        if segment in ("IMPORT", "EXPORT"):
            # Award spend is a domestic-demand signal; don't pollute trade cells.
            return []

        year = int(cell["year"])
        # period can be "YYYY-MM" or "YYYY-MM/YYYY-MM" — match either endpoint
        # to the cell year in Python after a coarse year-prefix prefilter.
        rows = fetch_rows(
            session,
            "SELECT source_id, country, value_usd, period "
            "FROM raw_procurement "
            "WHERE value_usd IS NOT NULL AND period LIKE :yp",
            {"yp": f"%{year}%"},
        )

        totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            if not country_matches(r.get("country"), cell.get("country")):
                continue
            # Confirm the award's start year is the cell year (the LIKE above is
            # only a coarse filter that could match the period's tail year).
            if year_of(r.get("period")) != year:
                continue
            totals[r["source_id"]] += Decimal(str(r["value_usd"]))
            counts[r["source_id"]] += 1

        results: list[dict[str, Any]] = []
        for source_id, total_usd in totals.items():
            est = usd_to_musd(total_usd)
            if est is None or est <= 0:
                continue
            results.append(self.row(
                estimate_usd_m=est,
                source_id=source_id,
                notes=(
                    f"Sum of {counts[source_id]} procurement award(s) in "
                    f"{cell.get('country')} ({year})"
                ),
            ))
        return results
