"""``comtrade_hs4_import`` — size a cell from import/export trade flows.

Sums the verbatim trade-flow values landed in ``raw_trade_flows`` for the
cell's HS codes, country (reporter) and year, in the direction implied by the
geography segment (IMPORT cells use import lines, EXPORT cells use export
lines). DOMESTIC cells have no cross-border trade record and are skipped.

One ``cell_triangulation`` row is emitted **per source** that contributed
rows (e.g. UN Comtrade and US Census can both cover a US import cell), so each
underlying source stays independently drillable. ``estimate_usd_m`` is the
summed ``value_usd`` converted to USD millions.

Tier A / source-class A / primary source. Reads only ``raw_trade_flows``.
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
    hs_code_set,
    hs_matches,
    period_prefix,
    segment_flow_predicate,
    usd_to_musd,
    year_of,
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.comtrade_hs4_import")


@register("comtrade_hs4_import")
class ComtradeHs4Import(Method):
    """Aggregate cross-border trade flows into a TAM estimate per source."""

    method_code = "comtrade_hs4_import"
    required_raw_tables = ["raw_trade_flows"]

    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        wanted_hs = hs_code_set(cell)
        if not wanted_hs:
            return []

        flow_predicate = segment_flow_predicate(cell.get("segment"))
        if flow_predicate is None:
            # DOMESTIC / non-trade segment — Comtrade cannot size it.
            return []

        year = int(cell["year"])
        rows = fetch_rows(
            session,
            "SELECT source_id, reporter, hs_code, flow, period, value_usd "
            "FROM raw_trade_flows "
            "WHERE value_usd IS NOT NULL AND period LIKE :yp",
            {"yp": period_prefix(year)},
        )

        totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        line_counts: dict[str, int] = defaultdict(int)
        for r in rows:
            if year_of(r.get("period")) != year:
                continue
            if not flow_predicate(r["flow"]):
                continue
            if not country_matches(r["reporter"], cell.get("country")):
                continue
            if not hs_matches(r["hs_code"], wanted_hs):
                continue
            totals[r["source_id"]] += Decimal(str(r["value_usd"]))
            line_counts[r["source_id"]] += 1

        results: list[dict[str, Any]] = []
        direction = "import" if cell.get("segment", "").upper() == "IMPORT" else "export"
        for source_id, total_usd in totals.items():
            est = usd_to_musd(total_usd)
            if est is None or est <= 0:
                continue
            results.append(self.row(
                estimate_usd_m=est,
                source_id=source_id,
                notes=(
                    f"Sum of {line_counts[source_id]} {direction} line(s) for "
                    f"HS {sorted(wanted_hs)} into {cell.get('country')} ({year})"
                ),
            ))
        return results
