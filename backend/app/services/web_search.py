"""Web-search fallback service (Q8) — LOW-capped, source-anchored, fail-safe.

Owns the *evidence-landing* half of the ``web_search_extraction`` method: when a
cell lacks structured coverage, it runs an agentic web search for the cell's
market size, then for each discovered result it

1. **auto-registers the URL's domain as a ``source``** row
   (``access_method='web_search'``, ``class='C'``, ``raw_table='raw_news'``), and
2. lands the result **verbatim** into ``raw_news`` (headline, url, snippet).

The companion method (``methods/web_search_extraction.py``) then reads those
``raw_news`` rows and emits a triangulation estimate — always Class C and hard
capped at LOW (it can seed triangulation but never manufacture HIGH/MEDIUM).

Guarantees:
* **Never fabricates.** With no ``ANTHROPIC_API_KEY``, or on any API/parse error,
  this is a silent no-op — no source, no snippet, no estimate.
* **Idempotent.** A (url) already in ``raw_news`` is not re-inserted; a source is
  upserted with ``ON CONFLICT DO NOTHING``.
* **Honest provenance.** Every landed row points at the real discovered URL, so
  the estimate stays drillable cell -> estimate -> source -> raw snippet.

Entry point consumed by the method: ``extract_for_cell(cell, session)``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text

from backend.app.config import settings

logger = logging.getLogger("grx10.services.web_search")

# Cap discovered results landed per cell per run (keeps the LOW-tier feed small).
_MAX_RESULTS = 5
_MODEL = "claude-opus-4-8"
_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}


def _enabled() -> bool:
    """Respect the per-engagement web-search toggle; default ON (Q8)."""
    for attr in ("WEB_SEARCH_FALLBACK_ENABLED", "web_search_fallback_enabled"):
        val = getattr(settings, attr, None)
        if val is not None:
            return bool(val)
    return True


def _source_id_for(url: str) -> tuple[str, str]:
    """Derive a stable (source_id, publisher) from a result URL's domain."""
    host = (urlparse(url).hostname or "unknown").lower()
    if host.startswith("www."):
        host = host[4:]
    slug = re.sub(r"[^a-z0-9]+", "_", host).strip("_") or "unknown"
    return f"websearch_{slug}", host


def _register_source(session: Any, source_id: str, publisher: str, url: str) -> None:
    """Idempotently upsert an auto-discovered web-search source row."""
    session.execute(
        text(
            """
            INSERT INTO sources
                (source_id, publisher, url_pattern, auth, class, raw_table,
                 access_method, discovered, enabled, notes)
            VALUES
                (:sid, :pub, :url, 'none', 'C', 'raw_news',
                 'web_search', true, true,
                 'Auto-registered by the web-search fallback (Q8, LOW-capped).')
            ON CONFLICT (source_id) DO NOTHING
            """
        ),
        {"sid": source_id, "pub": publisher, "url": url},
    )


def _already_landed(session: Any, url: str) -> bool:
    row = session.execute(
        text("SELECT 1 FROM raw_news WHERE url = :url LIMIT 1"), {"url": url}
    ).first()
    return row is not None


def _land_news(
    session: Any, source_id: str, url: str, headline: str, snippet: str, entity: str
) -> None:
    """Insert a verbatim raw_news row for a discovered result."""
    payload = {
        "url": url,
        "headline": headline,
        "snippet": snippet,
        "entity": entity,
        "origin": "web_search_fallback",
    }
    session.execute(
        text(
            """
            INSERT INTO raw_news
                (source_id, raw_json, headline, url, snippet, entity)
            VALUES
                (:sid, CAST(:payload AS JSONB), :headline, :url, :snippet, :entity)
            """
        ),
        {
            "sid": source_id,
            "payload": json.dumps(payload),
            "headline": headline[:1000],
            "url": url,
            "snippet": snippet[:8000],
            "entity": entity[:500],
        },
    )


def _query_for(cell: dict[str, Any]) -> str:
    sub = cell.get("subcategory_name") or ""
    country = cell.get("country") or ""
    year = cell.get("year") or ""
    return f"{sub} market size {country} {year} USD revenue".strip()


def _run_search(query: str) -> list[dict[str, str]]:
    """Call the Anthropic web-search tool; return [{url, title, snippet}].

    Any failure -> empty list (never raises into the pipeline).
    """
    api_key = getattr(settings, "ANTHROPIC_API_KEY", None)
    if not api_key:
        logger.debug("web_search: no ANTHROPIC_API_KEY — skipping (no fabrication).")
        return []
    try:
        import anthropic  # local import; optional dependency

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            tools=[_SEARCH_TOOL],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Find published market-size figures for: {query}. "
                        "Report the figure, the year, and cite the source page."
                    ),
                }
            ],
        )
        return _parse_results(msg)
    except Exception as exc:  # noqa: BLE001 — best-effort, never break the pipeline
        logger.warning("web_search: search failed (%s) — landing no evidence.", exc)
        return []


def _parse_results(msg: Any) -> list[dict[str, str]]:
    """Extract {url, title, snippet} from web_search_tool_result blocks + citations."""
    results: dict[str, dict[str, str]] = {}
    blocks = getattr(msg, "content", None) or []
    for block in blocks:
        btype = getattr(block, "type", None)
        # 1) Native web_search_tool_result blocks carry the result list.
        if btype == "web_search_tool_result":
            for item in getattr(block, "content", None) or []:
                url = getattr(item, "url", None)
                if not url:
                    continue
                results.setdefault(
                    url,
                    {
                        "url": url,
                        "title": getattr(item, "title", "") or "",
                        "snippet": (getattr(item, "encrypted_content", "") or "")[:0],
                    },
                )
        # 2) Text blocks carry citations whose cited_text is the usable snippet.
        if btype == "text":
            cited_text = getattr(block, "text", "") or ""
            for cit in getattr(block, "citations", None) or []:
                url = getattr(cit, "url", None)
                if not url:
                    continue
                snip = getattr(cit, "cited_text", "") or cited_text
                entry = results.setdefault(
                    url, {"url": url, "title": getattr(cit, "title", "") or "", "snippet": ""}
                )
                if snip and len(snip) > len(entry["snippet"]):
                    entry["snippet"] = snip
    return list(results.values())[:_MAX_RESULTS]


# --------------------------------------------------------------------------- #
# Public entry point (called by methods/web_search_extraction.py)
# --------------------------------------------------------------------------- #
def extract_for_cell(cell: dict[str, Any], session: Any) -> int:
    """Discover + land web-search evidence for one cell. Returns rows landed.

    Best-effort and idempotent. Safe to call every pipeline run. Returns 0 when
    disabled, unconfigured, or nothing new was found — never raises.
    """
    if not _enabled():
        return 0
    try:
        results = _run_search(_query_for(cell))
    except Exception as exc:  # noqa: BLE001
        logger.debug("web_search disabled by error: %s", exc)
        return 0

    landed = 0
    for r in results:
        url = r.get("url")
        snippet = (r.get("snippet") or "").strip()
        if not url or not snippet:
            continue  # no usable verbatim text -> don't land an empty row
        try:
            if _already_landed(session, url):
                continue
            source_id, publisher = _source_id_for(url)
            _register_source(session, source_id, publisher, url)
            _land_news(
                session,
                source_id=source_id,
                url=url,
                headline=r.get("title") or "",
                snippet=snippet,
                entity=str(cell.get("subcategory_name") or ""),
            )
            landed += 1
        except Exception as exc:  # noqa: BLE001 — isolate one bad result
            logger.debug("web_search: skipping result %s (%s)", url, exc)

    if landed:
        try:
            session.commit()
        except Exception:  # noqa: BLE001 — session may be autocommit/Connection
            pass
        logger.info("web_search: landed %d new snippet(s) for cell %s", landed, cell.get("cell_id"))
    return landed


# Aliases so the method's flexible lookup finds us regardless of name chosen.
populate_for_cell = extract_for_cell
search_cell = extract_for_cell
run_for_cell = extract_for_cell
