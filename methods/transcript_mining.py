"""``transcript_mining`` — extract revenue/market claims from earnings calls.

Scans ``raw_transcripts`` content for the cell year and pulls USD figures that
appear in a sentence which (a) mentions the subcategory and (b) is framed as a
market-size or revenue statement ("market", "revenue", "sales", "TAM"…). Each
such figure is a management-asserted estimate of the market the cell
represents.

Guardrails against fabrication / noise:

* A figure only counts inside a sentence that mentions a subcategory keyword,
  so unrelated guidance numbers are excluded.
* Only currency-marked figures (``$``/``USD``/``… dollars``) are lifted.
* Figures below a market-scale floor (USD 1m) are dropped as line noise.
* When a transcript yields several qualifying figures, the **largest** is taken
  as the market-size claim (sub-figures are usually segment slices of it).

One ``cell_triangulation`` row per contributing source; ``estimate_usd_m`` is
the chosen figure in millions. Tier B / class B.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.engine import Connection

from methods._common import (
    country_aliases,
    extract_usd_amounts,
    fetch_rows,
    period_prefix,
    sentences,
    subcategory_keywords,
    text_mentions,
    usd_to_musd,
    year_of,
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.transcript_mining")

# Words that frame a sentence as a market-size / revenue statement.
_SIZING_TERMS = (
    "market", "revenue", "sales", "tam", "demand", "billion", "million",
    "addressable", "opportunity",
)
# Below this (USD) a figure is treated as line noise, not a market size.
_MIN_USD = Decimal("1000000")


@register("transcript_mining")
class TranscriptMining(Method):
    """Extract subcategory-scoped market/revenue claims from transcripts."""

    method_code = "transcript_mining"
    required_raw_tables = ["raw_transcripts"]

    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        keywords = subcategory_keywords(cell)
        if not keywords:
            return []

        year = int(cell["year"])
        rows = fetch_rows(
            session,
            "SELECT source_id, company, content, period FROM raw_transcripts "
            "WHERE content IS NOT NULL AND period LIKE :yp",
            {"yp": period_prefix(year)},
        )

        country_terms = {a for a in country_aliases(cell.get("country")) if len(a) >= 4}

        # Per source, keep the single largest qualifying claim.
        best: dict[str, tuple[Decimal, str]] = {}
        for r in rows:
            if year_of(r.get("period")) != year:
                continue
            content = str(r.get("content") or "")
            for sentence in sentences(content):
                if not text_mentions(sentence, keywords):
                    continue
                if not text_mentions(sentence, _SIZING_TERMS):
                    continue
                # If the country is named anywhere it strengthens scope, but a
                # subcategory + sizing-term sentence already qualifies.
                amounts = [a for a in extract_usd_amounts(sentence) if a >= _MIN_USD]
                if not amounts:
                    continue
                claim = max(amounts)
                prior = best.get(r["source_id"])
                if prior is None or claim > prior[0]:
                    scope = "country-scoped" if text_mentions(sentence, country_terms) else "company-scoped"
                    best[r["source_id"]] = (claim, f"{r.get('company') or 'issuer'} ({scope})")

        results: list[dict[str, Any]] = []
        for source_id, (claim_usd, who) in best.items():
            est = usd_to_musd(claim_usd)
            if est is None or est <= 0:
                continue
            results.append(self.row(
                estimate_usd_m=est,
                source_id=source_id,
                notes=(
                    f"Market/revenue claim for {cell.get('subcategory_name')} "
                    f"({year}) mined from {who} earnings transcript"
                ),
            ))
        return results
