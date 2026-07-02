"""``top_down_industry_allocation`` — allocate a published TAM to a cell.

Reads a published market size from ``raw_industry_reports`` for the cell's
subcategory and year, then allocates it down to the cell's country.

Two allocation paths, in order of preference:

1. **Country-specific report** — when the report's ``market`` text already
   names the cell country, its ``tam_usd`` is taken directly (weight 1.0).
2. **Global report + macro weight** — when the report is global, the cell
   country's share is computed from GDP in ``raw_external_metrics`` (the
   country's GDP ÷ the summed GDP of all configured countries for that year).
   This is a defensible, source-backed split rather than an invented ratio.

If a report is global and no GDP data is available to weight it, the report is
skipped — the method never allocates with a fabricated share.

One ``cell_triangulation`` row per contributing report source; the macro
weight, when used, is recorded in the notes (the GDP source remains drillable
via the cell's other ``raw_external_metrics`` rows). Tier B / class B.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.engine import Connection

from methods._common import (
    country_aliases,
    fetch_rows,
    musd,
    period_prefix,
    subcategory_keywords,
    text_mentions,
    usd_to_musd,
    year_of,
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.top_down_industry_allocation")

# Indicator codes that denote nominal GDP across the connectors that land
# ``raw_external_metrics`` (World Bank "NY.GDP.MKTP.CD", or a plain "GDP").
_GDP_TOKENS = ("gdp", "ny.gdp.mktp")


@register("top_down_industry_allocation")
class TopDownIndustryAllocation(Method):
    """Allocate a published TAM to the cell by country (macro-weighted)."""

    method_code = "top_down_industry_allocation"
    required_raw_tables = ["raw_industry_reports"]

    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        keywords = subcategory_keywords(cell)
        if not keywords:
            return []

        year = int(cell["year"])
        reports = fetch_rows(
            session,
            "SELECT source_id, publisher, market, tam_usd, period "
            "FROM raw_industry_reports "
            "WHERE tam_usd IS NOT NULL AND period LIKE :yp",
            {"yp": period_prefix(year)},
        )
        reports = [
            r for r in reports
            if year_of(r.get("period")) == year
            and text_mentions(str(r.get("market") or ""), keywords)
        ]
        if not reports:
            return []

        country_weight = self._macro_weight(session, cell, year)

        results: list[dict[str, Any]] = []
        for r in reports:
            market = str(r.get("market") or "")
            tam_usd = Decimal(str(r["tam_usd"]))
            if self._names_country(market, cell):
                est = usd_to_musd(tam_usd)
                note = f"Country-specific published TAM for {cell.get('country')} ({year})"
            elif country_weight is not None:
                est = musd(usd_to_musd(tam_usd) * country_weight)
                note = (
                    f"Global TAM allocated to {cell.get('country')} at GDP weight "
                    f"{country_weight:.4f} ({year})"
                )
            else:
                # Global report, no macro weight available — do not fabricate.
                continue
            if est is None or est <= 0:
                continue
            results.append(self.row(
                estimate_usd_m=est,
                source_id=r["source_id"],
                notes=f"{note} — {r.get('publisher') or 'industry report'}",
            ))
        return results

    # ------------------------------------------------------------------ #
    @staticmethod
    def _names_country(market: str, cell: dict[str, Any]) -> bool:
        """True when the report's market text names the cell's country.

        Substring containment against the country's name/ISO aliases (a global
        report's market text — "global capacitor market" — names none).
        """
        low = market.lower()
        return any(alias in low for alias in country_aliases(cell.get("country")) if len(alias) >= 3)

    def _macro_weight(
        self, session: Connection, cell: dict[str, Any], year: int
    ) -> Decimal | None:
        """Country GDP ÷ total GDP across configured countries for ``year``.

        Returns ``None`` when GDP data is unavailable for the cell country (so
        a global report cannot be split and is skipped upstream).
        """
        rows = fetch_rows(
            session,
            "SELECT em.country, em.indicator, em.value, em.period "
            "FROM raw_external_metrics em "
            "WHERE em.value IS NOT NULL AND em.period LIKE :yp",
            {"yp": period_prefix(year)},
        )
        gdp_by_country: dict[str, Decimal] = {}
        for r in rows:
            if year_of(r.get("period")) != year:
                continue
            indicator = str(r.get("indicator") or "").lower()
            if not any(tok in indicator for tok in _GDP_TOKENS):
                continue
            ckey = str(r.get("country") or "").lower()
            try:
                val = Decimal(str(r["value"]))
            except (ValueError, ArithmeticError):
                continue
            # Keep the largest GDP figure per country key (dedupe annual dupes).
            if ckey and (ckey not in gdp_by_country or val > gdp_by_country[ckey]):
                gdp_by_country[ckey] = val
        if not gdp_by_country:
            return None

        # Find the cell country's GDP via its aliases.
        aliases = country_aliases(cell.get("country"))
        cell_gdp = next((v for k, v in gdp_by_country.items() if k in aliases), None)
        total_gdp = sum(gdp_by_country.values(), Decimal(0))
        if cell_gdp is None or total_gdp <= 0:
            return None
        return cell_gdp / total_gdp
