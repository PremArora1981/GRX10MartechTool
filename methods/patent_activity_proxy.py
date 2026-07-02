"""``patent_activity_proxy`` — patent filings as an output/innovation proxy.

Counts patent filings in ``raw_patents`` attributable to the cell's country and
year, then converts that count into a revenue-scale figure using a sourced
"USD revenue per patent filing" factor from the assumptions ledger. Patent
activity is a leading indicator of production capacity and R&D intensity; with a
calibrated per-filing value it becomes a (deliberately weak, Tier-C)
triangulation signal.

The conversion factor is never invented — it is read from an in-scope, in-effect
``assumptions`` row carrying its own ``source_id``. Without it (or without
filings) the method returns nothing.

Scope:

* Filings are matched by assignee country (ISO-2 in ``raw_patents.country``)
  and ``filing_date`` year.
* When the subcategory carries ``regulatory_codes`` / CPC-style hints, filings
  are additionally filtered to CPC codes that start with one of those prefixes;
  otherwise all of the country's filings for the year are counted.

The triangulation row is anchored on the **patent source** (``raw_patents``);
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
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.patent_activity_proxy")

_FACTOR_TERMS = ("per patent", "revenue per patent", "usd per patent", "patent value", "per filing")


@register("patent_activity_proxy")
class PatentActivityProxy(Method):
    """Country/year patent count × sourced USD-per-filing factor."""

    method_code = "patent_activity_proxy"
    required_raw_tables = ["raw_patents"]

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
            per_patent_usd = Decimal(str(assumption["numeric_value"]))
        except (ValueError, ArithmeticError):
            return []
        if per_patent_usd <= 0:
            return []

        # CPC prefixes from the subcategory's regulatory_codes (optional filter).
        cpc_prefixes = [str(c).strip().upper() for c in (cell.get("regulatory_codes") or []) if str(c).strip()]

        rows = fetch_rows(
            session,
            "SELECT source_id, country, cpc, filing_date FROM raw_patents "
            "WHERE filing_date IS NOT NULL "
            "AND CAST(EXTRACT(YEAR FROM filing_date) AS INT) = :yr",
            {"yr": year},
        )

        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            if not country_matches(r.get("country"), cell.get("country")):
                continue
            if cpc_prefixes:
                cpc = str(r.get("cpc") or "").upper()
                if not any(cpc.startswith(p) for p in cpc_prefixes):
                    continue
            counts[r["source_id"]] += 1

        results: list[dict[str, Any]] = []
        for source_id, n in counts.items():
            if n <= 0:
                continue
            est = musd((Decimal(n) * per_patent_usd) / Decimal("1000000"))
            if est is None or est <= 0:
                continue
            results.append(self.row(
                estimate_usd_m=est,
                source_id=source_id,
                notes=(
                    f"{n} patent filing(s) in {cell.get('country')} ({year}) × "
                    f"{per_patent_usd} USD/filing (assumption "
                    f"{assumption.get('assumption_id')}, src {assumption.get('source_id')})"
                ),
            ))
        return results
