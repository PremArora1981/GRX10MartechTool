"""``macro_scaling`` — scale a sized peer cell to this cell via a macro ratio.

When another geography's cell for the **same subcategory and year** has already
been sized (``cells.tam_revenue_usd_m`` is populated), this method projects an
estimate for the current cell by the ratio of a shared macro indicator (nominal
GDP) between the two countries::

    estimate = peer_TAM × (indicator_this_country / indicator_peer_country)

Both indicator values are read from ``raw_external_metrics`` for the cell year,
so the scaling factor is source-backed rather than assumed. The estimate is a
Tier-C triangulation support signal — it borrows another cell's evidence and
adjusts for economic size; it never stands alone as a primary number.

Mechanics / guardrails:

* Only **same-segment** peer cells are used (a DOMESTIC cell scales from another
  DOMESTIC cell), so trade-direction semantics are preserved.
* The peer with the **closest GDP** to this country is chosen (minimises the
  extrapolation distance).
* Requires GDP for both countries and a positive peer TAM, else returns nothing.
  On a first pipeline run no peer is sized yet, so the method is naturally a
  no-op until later runs — it degrades gracefully, never fabricates.

The triangulation row is anchored on the macro-indicator ``source_id``
(``raw_external_metrics``); the peer cell + ratio are described in the notes.
Tier C / class C.
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
    year_of,
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.macro_scaling")

_GDP_TOKENS = ("gdp", "ny.gdp.mktp")


@register("macro_scaling")
class MacroScaling(Method):
    """Scale a sized peer cell to this cell by a GDP ratio."""

    method_code = "macro_scaling"
    required_raw_tables = ["raw_external_metrics"]

    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        year = int(cell["year"])

        # --- GDP by country key for this year -----------------------------
        gdp_rows = fetch_rows(
            session,
            "SELECT source_id, country, indicator, value, period FROM raw_external_metrics "
            "WHERE value IS NOT NULL AND period LIKE :yp",
            {"yp": period_prefix(year)},
        )
        gdp: dict[str, tuple[Decimal, str]] = {}
        for r in gdp_rows:
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
            if val <= 0:
                continue
            if ckey and (ckey not in gdp or val > gdp[ckey][0]):
                gdp[ckey] = (val, r["source_id"])

        this_gdp = self._lookup(gdp, cell.get("country"))
        if this_gdp is None:
            return []
        this_gdp_val, gdp_source = this_gdp

        # --- sized peer cells: same subcategory + year, different geography --
        peers = fetch_rows(
            session,
            "SELECT c.cell_id, c.tam_revenue_usd_m, g.country, g.segment "
            "FROM cells c JOIN geographies g ON g.geography_id = c.geography_id "
            "WHERE c.subcategory_id = :sub AND c.year = :yr "
            "AND c.cell_id <> :cid AND c.tam_revenue_usd_m IS NOT NULL "
            "AND c.tam_revenue_usd_m > 0 AND g.segment = :seg",
            {"sub": cell["subcategory_id"], "yr": year, "cid": cell["cell_id"],
             "seg": cell.get("segment")},
        )
        if not peers:
            return []

        # Choose the peer whose GDP is closest to this country's (and for which
        # we actually have a GDP figure).
        best_peer = None
        best_gap: Decimal | None = None
        for p in peers:
            peer_gdp = self._lookup(gdp, p["country"])
            if peer_gdp is None:
                continue
            gap = abs(peer_gdp[0] - this_gdp_val)
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_peer = (p, peer_gdp[0])

        if best_peer is None:
            return []
        peer, peer_gdp_val = best_peer
        ratio = this_gdp_val / peer_gdp_val
        est = musd(Decimal(str(peer["tam_revenue_usd_m"])) * ratio)
        if est is None or est <= 0:
            return []

        return [self.row(
            estimate_usd_m=est,
            source_id=gdp_source,
            notes=(
                f"Scaled peer cell {peer['cell_id']} ({peer['country']}, "
                f"TAM {peer['tam_revenue_usd_m']}m) by GDP ratio "
                f"{ratio:.4f} to {cell.get('country')} ({year})"
            ),
        )]

    @staticmethod
    def _lookup(gdp: dict[str, tuple[Decimal, str]], country: Any) -> tuple[Decimal, str] | None:
        aliases = country_aliases(country)
        for key, val in gdp.items():
            if key in aliases:
                return val
        return None
