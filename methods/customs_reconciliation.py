"""``customs_reconciliation`` — apparent-consumption cross-check.

Computes a market size from the trade-balance identity::

    apparent consumption = domestic production + imports − exports

for the cell's country, HS codes and year. Imports and exports come from
``raw_trade_flows``; domestic production comes from ``raw_external_metrics``
(a statistical-yearbook "production"/"output" indicator in USD). This is the
classic customs reconciliation: it reconciles cross-border flows against
reported production to size the addressable domestic market.

Scope:

* Applies to **DOMESTIC** cells (apparent consumption is a domestic-market
  quantity). IMPORT/EXPORT cells are sized directly by ``comtrade_hs4_import``.
* Requires a production figure — without it the identity is incomplete, so the
  cell is skipped rather than approximated.

The estimate is anchored on the **production source** (``raw_external_metrics``)
as ``source_id`` (the reconciliation hinges on it); the trade legs are summed
across whatever trade sources are present and described in the notes. Tier B /
class B. Reads ``raw_trade_flows`` + ``raw_external_metrics``.
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
    flow_is_export,
    flow_is_import,
    hs_code_set,
    hs_matches,
    period_prefix,
    usd_to_musd,
    year_of,
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.customs_reconciliation")

# Indicator tokens that denote domestic production / output value.
_PRODUCTION_TOKENS = ("production", "output", "manufactur", "gross_output", "shipments_value")


@register("customs_reconciliation")
class CustomsReconciliation(Method):
    """Apparent consumption = production + imports − exports."""

    method_code = "customs_reconciliation"
    required_raw_tables = ["raw_trade_flows", "raw_external_metrics"]

    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        if str(cell.get("segment") or "").upper() != "DOMESTIC":
            return []
        wanted_hs = hs_code_set(cell)
        if not wanted_hs:
            return []

        year = int(cell["year"])

        # --- trade legs (summed across all trade sources) ------------------
        trade = fetch_rows(
            session,
            "SELECT reporter, hs_code, flow, value_usd, period FROM raw_trade_flows "
            "WHERE value_usd IS NOT NULL AND period LIKE :yp",
            {"yp": period_prefix(year)},
        )
        imports = Decimal(0)
        exports = Decimal(0)
        for r in trade:
            if year_of(r.get("period")) != year:
                continue
            if not country_matches(r["reporter"], cell.get("country")):
                continue
            if not hs_matches(r["hs_code"], wanted_hs):
                continue
            val = Decimal(str(r["value_usd"]))
            if flow_is_import(r["flow"]):
                imports += val
            elif flow_is_export(r["flow"]):
                exports += val

        # --- production legs (one estimate per production source) ----------
        metrics = fetch_rows(
            session,
            "SELECT source_id, country, indicator, value, unit, period "
            "FROM raw_external_metrics "
            "WHERE value IS NOT NULL AND period LIKE :yp",
            {"yp": period_prefix(year)},
        )
        production_by_source: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        for r in metrics:
            if year_of(r.get("period")) != year:
                continue
            indicator = str(r.get("indicator") or "").lower()
            if not any(tok in indicator for tok in _PRODUCTION_TOKENS):
                continue
            if not country_matches(r.get("country"), cell.get("country")):
                continue
            production_by_source[r["source_id"]] += Decimal(str(r["value"]))

        if not production_by_source:
            return []

        results: list[dict[str, Any]] = []
        for source_id, production in production_by_source.items():
            apparent = production + imports - exports
            est = usd_to_musd(apparent)
            if est is None or est <= 0:
                # Negative/zero apparent consumption is not a defensible TAM.
                continue
            results.append(self.row(
                estimate_usd_m=est,
                source_id=source_id,
                notes=(
                    f"Apparent consumption = production({usd_to_musd(production)}m) "
                    f"+ imports({usd_to_musd(imports)}m) − exports({usd_to_musd(exports)}m) "
                    f"for {cell.get('country')} HS {sorted(wanted_hs)} ({year})"
                ),
            ))
        return results
