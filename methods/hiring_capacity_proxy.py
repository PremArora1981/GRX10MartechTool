"""``hiring_capacity_proxy`` — hiring/job-posting volume as a capacity signal.

Aggregates hiring-related signals in ``raw_signals`` (job openings, postings,
hires) for the cell's country and year, then converts the volume into a
revenue-scale figure using a sourced "USD revenue per job/posting" factor from
the assumptions ledger. Hiring intensity is a forward indicator of capacity
expansion; with a calibrated factor it contributes a Tier-C triangulation
signal.

The conversion factor is read from an in-scope, in-effect ``assumptions`` row
with its own ``source_id`` — never invented. Without it (or without hiring
signals) the method returns nothing.

The triangulation row is anchored on the **signal source** (``raw_signals``);
the conversion assumption and its source are recorded in the notes. Tier C /
class C.
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
    get_assumption,
    musd,
    period_prefix,
    year_of,
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.hiring_capacity_proxy")

# signal_type values that denote hiring/labour-demand activity.
_HIRING_TOKENS = (
    "hir", "job", "opening", "posting", "vacanc", "headcount", "recruit", "employ",
)
_FACTOR_TERMS = (
    "per hire", "per job", "per posting", "revenue per employee",
    "usd per hire", "usd per job", "per opening",
)


@register("hiring_capacity_proxy")
class HiringCapacityProxy(Method):
    """Hiring volume × sourced USD-per-hire factor."""

    method_code = "hiring_capacity_proxy"
    required_raw_tables = ["raw_signals"]

    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        year = int(cell["year"])

        assumption = get_assumption(
            session,
            subcategory_id=cell.get("subcategory_id"),
            geography_id=cell.get("geography_id"),
            year=year,
            terms=_FACTOR_TERMS,
        )
        if not assumption:
            return []
        try:
            per_unit_usd = Decimal(str(assumption["numeric_value"]))
        except (ValueError, ArithmeticError):
            return []
        if per_unit_usd <= 0:
            return []

        rows = fetch_rows(
            session,
            "SELECT source_id, country, signal_type, value, period FROM raw_signals "
            "WHERE value IS NOT NULL AND period LIKE :yp",
            {"yp": period_prefix(year)},
        )

        volume_by_source: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        for r in rows:
            if year_of(r.get("period")) != year:
                continue
            signal_type = str(r.get("signal_type") or "").lower()
            if not any(tok in signal_type for tok in _HIRING_TOKENS):
                continue
            if not country_matches(r.get("country"), cell.get("country")):
                continue
            volume_by_source[r["source_id"]] += Decimal(str(r["value"]))

        results: list[dict[str, Any]] = []
        for source_id, volume in volume_by_source.items():
            if volume <= 0:
                continue
            est = musd((volume * per_unit_usd) / Decimal("1000000"))
            if est is None or est <= 0:
                continue
            results.append(self.row(
                estimate_usd_m=est,
                source_id=source_id,
                notes=(
                    f"Hiring volume {volume} × {per_unit_usd} USD/unit "
                    f"(assumption {assumption.get('assumption_id')}, "
                    f"src {assumption.get('source_id')}) for {cell.get('country')} ({year})"
                ),
            ))
        return results
