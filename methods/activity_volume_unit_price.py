"""``activity_volume_unit_price`` — size a cell as activity count × unit price.

Multiplies an activity/volume count from ``raw_external_metrics`` (procedures,
transactions, shipments, units produced …) for the cell's country and year by
an average selling/unit price drawn from the **assumptions ledger**. This is
the canonical bottom-up sizing for markets where a physical or transactional
volume is observable but its monetary value is not directly reported.

The unit price is never invented: it is read from an in-scope, in-effect
``assumptions`` row (scoped to the subcategory/geography) that carries its own
``source_id``. When no such assumption exists, or no volume metric is present,
the method returns nothing rather than guessing a price.

``estimate_usd_m = volume × unit_price_usd ÷ 1e6``. The triangulation row is
anchored on the **volume source** (``raw_external_metrics``); the price
assumption and its source are recorded in the notes (and remain drillable via
the assumptions ledger). Tier B / class B.
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
    subcategory_keywords,
    year_of,
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.activity_volume_unit_price")

# Indicator tokens denoting a count/volume metric (not a value metric).
_VOLUME_TOKENS = (
    "volume", "count", "units", "procedures", "transactions", "shipments",
    "quantity", "throughput", "installations", "registrations",
)
# Terms used to locate an ASP / unit-price assumption in the ledger.
_PRICE_TERMS = ("asp", "unit price", "price per", "usd per", "average selling", "per unit")


@register("activity_volume_unit_price")
class ActivityVolumeUnitPrice(Method):
    """Volume (from metrics) × unit price (from assumptions ledger)."""

    method_code = "activity_volume_unit_price"
    required_raw_tables = ["raw_external_metrics"]

    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        year = int(cell["year"])

        # Unit price (ASP) must come from a sourced assumption — no fabrication.
        assumption = get_assumption(
            session,
            subcategory_id=cell.get("subcategory_id"),
            geography_id=cell.get("geography_id"),
            year=year,
            terms=_PRICE_TERMS,
        )
        if not assumption:
            return []
        try:
            unit_price = Decimal(str(assumption["numeric_value"]))
        except (ValueError, ArithmeticError):
            return []
        if unit_price <= 0:
            return []

        rows = fetch_rows(
            session,
            "SELECT source_id, country, indicator, value, unit, period "
            "FROM raw_external_metrics "
            "WHERE value IS NOT NULL AND period LIKE :yp",
            {"yp": period_prefix(year)},
        )

        # The subcategory keywords help confirm the indicator is on-topic when
        # the indicator string carries a descriptive label.
        keywords = subcategory_keywords(cell)
        # A subcategory-scoped assumption (specificity >= 2) already pins the
        # ASP to this subcategory, so an opaque indicator code is acceptable;
        # otherwise the indicator label must itself be on-topic.
        sub_scoped = assumption.get("specificity", 0) >= 2
        volume_by_source: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        for r in rows:
            if year_of(r.get("period")) != year:
                continue
            indicator = str(r.get("indicator") or "").lower()
            if not any(tok in indicator for tok in _VOLUME_TOKENS):
                continue
            on_topic = (not keywords) or sub_scoped or any(k in indicator for k in keywords)
            if not on_topic:
                continue
            if not country_matches(r.get("country"), cell.get("country")):
                continue
            volume_by_source[r["source_id"]] += Decimal(str(r["value"]))

        results: list[dict[str, Any]] = []
        for source_id, volume in volume_by_source.items():
            if volume <= 0:
                continue
            est = musd((volume * unit_price) / Decimal("1000000"))
            if est is None or est <= 0:
                continue
            results.append(self.row(
                estimate_usd_m=est,
                source_id=source_id,
                notes=(
                    f"Volume {volume} × ASP {unit_price} "
                    f"{assumption.get('unit') or 'USD/unit'} "
                    f"(assumption {assumption.get('assumption_id')}, "
                    f"src {assumption.get('source_id')}) for {cell.get('country')} ({year})"
                ),
            ))
        return results
