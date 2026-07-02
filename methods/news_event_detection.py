"""``news_event_detection`` — detect catalysts from news/filings.

Unlike the sizing methods, this one does **not** produce a TAM estimate: it
mines ``raw_news`` for market-moving events (M&A, capacity changes, regulation,
supply disruptions) relevant to a cell and records them in the ``catalysts``
table (Layer 4). It therefore returns ``[]`` from :meth:`estimate` — a catalyst
is a qualitative decision input, not a number that belongs in the confidence
triangulation.

Detection:

* A news row is relevant to a cell when its headline/snippet mentions a
  subcategory keyword **or** the cell's country, and contains a catalyst
  trigger word.
* ``catalyst_type`` and ``impact_direction`` are classified from the trigger
  vocabulary (e.g. "shortage"→supply/negative, "expansion"→capacity/positive).
* Only events published within the cell year are attached, so a catalyst is
  tied to the period it bears on.

Idempotency: a catalyst is inserted only when no existing row shares the same
``(cell_id, source_id, url)`` (the URL is embedded in ``description``), so
re-running the pipeline never duplicates a catalyst. Every catalyst carries the
news ``source_id`` (the invariant: no fact row without a source). Tier C /
class C.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from methods._common import (
    country_aliases,
    fetch_rows,
    subcategory_keywords,
    text_mentions,
)
from methods.base import Method
from methods.registry import register

logger = logging.getLogger("grx10.methods.news_event_detection")

# trigger word -> (catalyst_type, impact_direction)
_CATALYST_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("acquir", "acquisition", "merger", "merges", "buyout", "takeover"), "M&A", "positive"),
    (("expansion", "expand", "new plant", "new factory", "new fab", "capacity",
      "groundbreaking", "ramp"), "capacity_expansion", "positive"),
    (("shortage", "disruption", "supply constraint", "force majeure", "outage"),
     "supply_disruption", "negative"),
    (("tariff", "sanction", "ban ", "export control", "regulation", "antitrust",
      "investigation", "recall", "lawsuit"), "regulation", "negative"),
    (("partnership", "joint venture", "contract win", "award", "launch",
      "approval", "record"), "commercial", "positive"),
]


@register("news_event_detection")
class NewsEventDetection(Method):
    """Detect and persist catalysts from news; emits no triangulation rows."""

    method_code = "news_event_detection"
    required_raw_tables = ["raw_news"]

    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        keywords = subcategory_keywords(cell)
        country_terms = {a for a in country_aliases(cell.get("country")) if len(a) >= 4}
        scope_terms = keywords | country_terms
        if not scope_terms:
            return []

        year = int(cell["year"])
        rows = fetch_rows(
            session,
            "SELECT source_id, headline, url, snippet, published_at "
            "FROM raw_news "
            "WHERE published_at IS NOT NULL "
            "AND CAST(EXTRACT(YEAR FROM published_at) AS INT) = :yr",
            {"yr": year},
        )

        inserted = 0
        for r in rows:
            blob = f"{r.get('headline') or ''} {r.get('snippet') or ''}".strip()
            if not blob:
                continue
            if not text_mentions(blob, scope_terms):
                continue
            classified = self._classify(blob)
            if classified is None:
                continue
            catalyst_type, direction = classified
            url = str(r.get("url") or "")
            description = self._describe(r.get("headline"), url)

            # Idempotent: skip if this URL is already a catalyst for this cell+source.
            exists = session.execute(text(
                "SELECT 1 FROM catalysts WHERE cell_id = :cid AND source_id = :sid "
                "AND description LIKE :pat LIMIT 1"),
                {"cid": cell["cell_id"], "sid": r["source_id"],
                 "pat": f"%{url}%" if url else f"%{description[:60]}%"}).first()
            if exists:
                continue

            session.execute(text(
                "INSERT INTO catalysts "
                "(cell_id, catalyst_type, impact_direction, expected_quarter, "
                " description, source_id) "
                "VALUES (:cid, :ctype, :dir, :eq, :desc, :sid)"),
                {"cid": cell["cell_id"], "ctype": catalyst_type, "dir": direction,
                 "eq": self._quarter(r.get("published_at")),
                 "desc": description, "sid": r["source_id"]})
            inserted += 1

        if inserted:
            logger.info("news_event_detection: +%d catalyst(s) for cell %s",
                        inserted, cell["cell_id"])
        # No triangulation estimate — catalysts are a Layer-4 decision input.
        return []

    # ------------------------------------------------------------------ #
    @staticmethod
    def _classify(blob: str) -> tuple[str, str] | None:
        low = blob.lower()
        for triggers, ctype, direction in _CATALYST_RULES:
            if any(t in low for t in triggers):
                return ctype, direction
        return None

    @staticmethod
    def _describe(headline: Any, url: str) -> str:
        head = str(headline or "Market event").strip()
        return f"{head} ({url})" if url else head

    @staticmethod
    def _quarter(published_at: Any) -> str | None:
        """Derive a ``YYYY-Qn`` label from a publication datetime."""
        if published_at is None:
            return None
        try:
            month = published_at.month
            year = published_at.year
        except AttributeError:
            return None
        return f"{year}-Q{(month - 1) // 3 + 1}"
