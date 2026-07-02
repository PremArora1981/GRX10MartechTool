"""``web_search_extraction`` — agentic web-search fallback (Class C, LOW-capped).

Fills a triangulation gap when no structured connector covers a cell. The
heavy lifting — running the agentic search, auto-registering each discovered
URL as a ``source`` (``access_method = 'web_search'``, ``class = 'C'``) and
landing the extraction snippet verbatim into ``raw_news`` — is owned by the
**web-search service** (``backend.app.services.web_search``). This method:

1. Best-effort asks that service to populate web-search evidence for the cell
   (when the service module is deployed); it degrades silently when it is not.
2. Reads the web-search-sourced ``raw_news`` snippets for the cell and extracts
   a published market-size figure that matches the subcategory + country + year.

Per the locked Q8 decision the resulting estimate is **always Class C and hard-
capped at LOW** — it can fill blanks and seed triangulation but can never on its
own manufacture HIGH/MEDIUM confidence (the active validation profile requires
multiple distinct methods / source classes / a Tier-A primary, which a lone
class-C web-search signal cannot satisfy). The figure is real and source-backed:
its ``source_id`` is the auto-registered discovered URL, fully drillable to the
verbatim snippet in ``raw_news``.

Tier C / class C / ``confidence_cap = low``.
"""

from __future__ import annotations

import importlib
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.engine import Connection

from methods._common import (
    country_aliases,
    extract_usd_amounts,
    fetch_rows,
    musd,
    subcategory_keywords,
    text_mentions,
    year_of,
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.web_search_extraction")

# Below this (USD) a figure is line noise, not a market-size statement.
_MIN_USD = Decimal("1000000")
# Sizing-frame words that mark a snippet as a market-size statement.
_SIZING_TERMS = (
    "market", "market size", "valued at", "tam", "revenue", "industry",
    "forecast", "reach", "worth", "billion", "million",
)


@register("web_search_extraction")
class WebSearchExtraction(Method):
    """Extract a market-size figure from web-search-sourced snippets (LOW cap)."""

    method_code = "web_search_extraction"
    required_raw_tables = ["raw_news"]

    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        keywords = subcategory_keywords(cell)
        if not keywords:
            return []
        year = int(cell["year"])

        # 1) Best-effort: let the web-search service populate fresh evidence.
        self._invoke_service(cell, session)

        # 2) Read web-search-sourced news snippets (sources flagged web_search).
        rows = fetch_rows(
            session,
            "SELECT n.source_id, n.headline, n.snippet, n.url, n.published_at "
            "FROM raw_news n JOIN sources s ON s.source_id = n.source_id "
            "WHERE s.access_method = 'web_search' "
            "AND (n.snippet IS NOT NULL OR n.headline IS NOT NULL)",
            {},
        )

        country_terms = {a for a in country_aliases(cell.get("country")) if len(a) >= 4}

        # Per source, keep the largest qualifying market-size figure.
        best: dict[str, tuple[Decimal, str]] = {}
        for r in rows:
            blob = f"{r.get('headline') or ''} {r.get('snippet') or ''}".strip()
            if not blob:
                continue
            # Year relevance: if the snippet carries a year, require the cell's.
            snippet_year = year_of(blob)
            if snippet_year is not None and snippet_year != year:
                continue
            if not text_mentions(blob, keywords):
                continue
            if country_terms and not text_mentions(blob, country_terms):
                # A country-specific cell needs the country named in the snippet.
                if str(cell.get("segment") or "").upper() != "DOMESTIC":
                    continue
            if not text_mentions(blob, _SIZING_TERMS):
                continue
            amounts = [a for a in extract_usd_amounts(blob) if a >= _MIN_USD]
            if not amounts:
                continue
            figure = max(amounts)
            prior = best.get(r["source_id"])
            if prior is None or figure > prior[0]:
                best[r["source_id"]] = (figure, str(r.get("url") or ""))

        results: list[dict[str, Any]] = []
        for source_id, (figure_usd, url) in best.items():
            est = musd(figure_usd / Decimal("1000000"))
            if est is None or est <= 0:
                continue
            results.append(self.row(
                estimate_usd_m=est,
                source_id=source_id,
                notes=(
                    f"Web-search extracted market size for "
                    f"{cell.get('subcategory_name')} in {cell.get('country')} "
                    f"({year}) [Class C, LOW-capped] — {url}"
                ),
            ))
        return results

    # ------------------------------------------------------------------ #
    @staticmethod
    def _invoke_service(cell: dict[str, Any], session: Connection) -> None:
        """Ask the web-search service to land evidence for the cell, if present.

        The service owns URL discovery, source auto-registration and raw_news
        landing. This is strictly best-effort: any failure (module absent, no
        API key, network error) is swallowed — the method then simply reads
        whatever web-search evidence already exists, and returns nothing if
        there is none. We never fabricate a snippet or a source here.
        """
        try:
            svc = importlib.import_module("backend.app.services.web_search")
        except Exception:  # noqa: BLE001 — service not deployed yet
            return
        for attr in ("extract_for_cell", "populate_for_cell", "search_cell", "run_for_cell"):
            fn = getattr(svc, attr, None)
            if callable(fn):
                try:
                    fn(cell=cell, session=session)
                except TypeError:
                    try:
                        fn(cell, session)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("web_search service %s failed: %s", attr, exc)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("web_search service %s failed: %s", attr, exc)
                return
