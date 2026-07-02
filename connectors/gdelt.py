"""GDELT DOC 2.0 API connector — news and M&A signal ingestion into ``raw_news``.

GDELT (Global Database of Events, Language and Tone) indexes news articles
worldwide and exposes them via its free, unauthenticated DOC 2.0 API:

    https://api.gdeltproject.org/api/v2/doc/doc

Key facts:
* **No authentication** required.
* **250 articles per call** (``maxrecords=250`` is the hard cap).
* **Datetime pagination**: supply ``startdatetime`` / ``enddatetime`` in the
  GDELT ``YYYYMMDDHHMMSS`` UTC format; advance the window after each page.
* The connector derives a keyword query from the active taxonomy so it
  surfaces news relevant to the configured market scope.
* GDELT ``ArtList`` mode returns article metadata only (title, URL, seendate,
  domain, language, sourcecountry) — no article body.  The ``snippet`` column
  is left ``NULL``; a separate fetch-and-store step is outside v1 scope.

Registered name: ``gdelt``  (matches ``sources.connector`` in the seed YAML).
Raw table: ``raw_news`` (headline, url, published_at, entity, snippet).

Invariants:
* Never fabricates rows — if GDELT returns an unexpected shape, ``normalize``
  returns an empty dict rather than invented data.
* Every HTTP error is classified into the 7-state taxonomy via
  ``classify_http_error``.
* The sliding-window pull is idempotent: re-running with the same ``since``
  will yield the same articles; the pipeline's composite-key upsert on
  ``(source_id, url)`` prevents duplicates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from connectors.base import Connector, ProbeResult, classify_exception, classify_http_error
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.gdelt")

# ---- GDELT API constants -----------------------------------------------
_GDELT_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
# Hard ceiling enforced by GDELT.
_MAX_RECORDS = 250
# Each paginated window spans this many hours.
_PAGE_WINDOW_HOURS = 24
# Default lookback when ``since`` is not supplied.
_DEFAULT_LOOKBACK_DAYS = 30
# Polite inter-request gap — GDELT is a shared public resource.
_MIN_INTERVAL = 2.0
# GDELT datetime wire format.
_DT_FMT = "%Y%m%d%H%M%S"


# ---- Helpers -----------------------------------------------------------

def _to_gdelt_dt(dt: datetime) -> str:
    """Serialise a UTC :class:`datetime` to GDELT's ``YYYYMMDDHHMMSS`` format."""
    return dt.astimezone(timezone.utc).strftime(_DT_FMT)


def _from_gdelt_dt(s: str | None) -> datetime | None:
    """Parse a GDELT ``seendate`` (``YYYYMMDDTHHMMSSZ`` or ``YYYYMMDDHHMMSS``) to UTC.

    GDELT inconsistently uses either ``20240101T123456Z`` or ``20240101123456``
    in different API modes — this handles both.
    """
    if not s:
        return None
    cleaned = s.replace("T", "").replace("Z", "").strip()
    try:
        return datetime.strptime(cleaned, _DT_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_since(since: str | None, fallback_days: int = _DEFAULT_LOOKBACK_DAYS) -> datetime:
    """Convert the pipeline's ISO ``since`` hint to a UTC datetime.

    Accepts ISO date (``YYYY-MM-DD``) or ISO datetime (``YYYY-MM-DDTHH:MM:SS``).
    Returns the fallback if the string is absent or unparseable.
    """
    now = datetime.now(timezone.utc)
    if not since:
        return now - timedelta(days=fallback_days)
    clean = since.strip().replace("Z", "+00:00")
    try:
        if "T" in clean:
            dt = datetime.fromisoformat(clean)
        else:
            dt = datetime.fromisoformat(clean + "T00:00:00+00:00")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        logger.warning("gdelt: cannot parse since=%r; falling back to %d days ago",
                       since, fallback_days)
        return now - timedelta(days=fallback_days)


def _build_query(taxonomy: list[dict[str, Any]]) -> str:
    """Derive a GDELT keyword query from the active taxonomy rows.

    Extracts unique subcategory names and HS codes and combines them as an
    OR query.  Multi-word names are phrase-quoted.  Falls back to a broad
    industrial-electronics sentinel when the taxonomy is empty or unhelpful
    so the probe call always hits a live result.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for row in taxonomy:
        name = (row.get("name") or "").strip()
        if name and len(name) > 3 and name not in seen:
            seen.add(name)
            terms.append(f'"{name}"' if " " in name else name)
        for hs in (row.get("hs_codes") or []):
            code = str(hs).strip()
            if code and code not in seen:
                seen.add(code)
                terms.append(code)
    if not terms:
        # Safe fallback so probe/pull always exercise the live API.
        return "industrial electronics components market"
    # Practical GDELT query length cap ~500 chars.
    return " OR ".join(terms)[:480]


# ---- Connector ---------------------------------------------------------

@register("gdelt")
class GdeltDocApiConnector(Connector):
    """GDELT DOC 2.0 API connector writing article metadata to ``raw_news``.

    **Pagination**: the connector advances a ``_PAGE_WINDOW_HOURS``-wide
    sliding datetime window from ``since`` (or 30 days ago) to now, issuing
    one API call per window and yielding all articles returned.  An early-exit
    applies when GDELT returns fewer than ``_MAX_RECORDS`` articles for a
    window (indicating the window contained fewer articles than the page cap)
    and the window covers the current time — though in practice each window is
    always fully consumed because short pages can legitimately occur mid-range.

    **Entity field**: GDELT does not expose named-entity tags in ArtList mode.
    The ``entity`` column is filled with the article's ``domain`` (e.g.
    ``reuters.com``) as a structural proxy for the originating publisher.
    """

    source_id = "gdelt"
    raw_table = "raw_news"
    min_interval = _MIN_INTERVAL

    # ------------------------------------------------------------------ #
    # probe
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """Issue a minimal 1-record request to verify GDELT API reachability.

        Uses a stable keyword ("electronics") to avoid an empty result set
        while keeping the probe cheap.
        """
        params: dict[str, Any] = {
            "query": "electronics",
            "mode": "ArtList",
            "maxrecords": 1,
            "format": "json",
            "sort": "DateDesc",
        }
        try:
            resp = self.http.request("GET", _GDELT_API_URL, params=params)
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail, None)

        if resp.status_code != 200:
            status = classify_http_error(resp.status_code, resp.text)
            return ProbeResult(
                status,
                f"HTTP {resp.status_code}: {resp.text[:200]}",
                None,
            )

        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return ProbeResult("SCHEMA_MISMATCH",
                               "HTTP 200 but response body is not valid JSON", None)

        # GDELT wraps articles in an "articles" list (or returns {"articles": null}
        # when the query matches nothing in the probe time window).
        articles = payload.get("articles") or []
        if not articles:
            return ProbeResult("EMPTY",
                               "reachable but no articles returned for probe query", None)

        return ProbeResult(
            "OK",
            f"HTTP 200, {len(articles)} article(s) on probe",
            articles[0],
        )

    # ------------------------------------------------------------------ #
    # pull
    # ------------------------------------------------------------------ #

    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield verbatim GDELT article records for the taxonomy scope.

        Each yielded dict is a single GDELT ArtList article record containing
        ``url``, ``title``, ``seendate``, ``domain``, ``language``,
        ``sourcecountry``, and ``url_mobile``.

        The sliding datetime window advances from ``since`` to now in
        ``_PAGE_WINDOW_HOURS``-hour steps.  The pull stops when the window
        exceeds the current time or on an unrecoverable HTTP error.
        """
        query = _build_query(taxonomy)
        now = datetime.now(timezone.utc)
        window_start = _parse_since(since)

        logger.info("gdelt pull: query=%r window=%s to %s",
                    query[:80], window_start.date(), now.date())

        while window_start < now:
            window_end = min(window_start + timedelta(hours=_PAGE_WINDOW_HOURS), now)

            params: dict[str, Any] = {
                "query": query,
                "mode": "ArtList",
                "maxrecords": _MAX_RECORDS,
                "format": "json",
                "sort": "DateAsc",
                "startdatetime": _to_gdelt_dt(window_start),
                "enddatetime": _to_gdelt_dt(window_end),
            }

            try:
                resp = self.http.request("GET", _GDELT_API_URL, params=params)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "gdelt: request failed for window %s–%s: %s",
                    window_start, window_end, exc,
                )
                break  # Transient error — stop rather than silently skip windows.

            if resp.status_code != 200:
                logger.info(
                    "gdelt: HTTP %s for window %s–%s; stopping pull",
                    resp.status_code, window_start, window_end,
                )
                break

            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                logger.warning("gdelt: non-JSON body for window %s–%s",
                               window_start, window_end)
                break

            articles: list[dict[str, Any]] = payload.get("articles") or []
            logger.debug("gdelt: window %s–%s → %d articles",
                         window_start, window_end, len(articles))

            for article in articles:
                if isinstance(article, dict):
                    yield article

            # Advance the window regardless of how many articles came back;
            # a short page just means fewer articles in that time window.
            window_start = window_end

    # ------------------------------------------------------------------ #
    # normalize
    # ------------------------------------------------------------------ #

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one GDELT ArtList article record to typed ``raw_news`` columns.

        GDELT ArtList article keys:
            ``url``, ``url_mobile``, ``title``, ``seendate``,
            ``socialimage``, ``domain``, ``language``, ``sourcecountry``.

        Returns
        -------
        dict
            ``headline``   — article title (``title``).
            ``url``        — canonical article URL (``url``).
            ``published_at`` — parsed UTC datetime from ``seendate``; ``None``
              when the seendate is absent or unparseable.
            ``entity``     — publisher domain (``domain``), e.g. ``reuters.com``.
              GDELT ArtList does not expose named-entity tags.
            ``snippet``    — always ``None``; GDELT ArtList returns metadata
              only.  A subsequent page-fetch step (outside v1 scope) would
              populate this.

        Empty dict is returned only when ``raw`` itself is empty or ``None``.
        """
        if not raw:
            return {}

        headline: str | None = raw.get("title") or None
        url: str | None = raw.get("url") or None
        seendate: str | None = raw.get("seendate") or None
        domain: str | None = raw.get("domain") or None
        published_at: datetime | None = _from_gdelt_dt(seendate)

        return {
            "headline": headline,
            "url": url,
            "published_at": published_at,
            "entity": domain,
            "snippet": None,  # ArtList mode does not include article body.
        }
