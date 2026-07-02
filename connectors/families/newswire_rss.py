"""Newswire RSS connector family — PR Newswire, Business Wire, GlobeNewswire.

One :class:`NewswireRssFamilyConnector` base drives three concrete connectors
through the same feedparser-based RSS ingestion path.  Each registered
connector points its ``sources.url_pattern`` to its RSS endpoint; all share
the same ``probe`` / ``pull`` / ``normalize`` implementation.

Concrete ``source_id`` values (match ``sources`` table):
    ``prnewswire_rss``    — PR Newswire all-press-releases feed.
    ``businesswire_rss``  — Business Wire all-news feed.
    ``globenewswire_rss`` — GlobeNewswire industrial-sector feed.

Override ``url_pattern`` on the ``sources`` row to target a topic-specific
feed (e.g. ``/rss/electronics-news-releases-list.rss`` for PR Newswire).

Design notes:
* **RSS vs REST**: RSS feeds are fetched via our standard :class:`HttpClient`
  so they inherit throttling, retry/backoff, and the descriptive User-Agent
  that some CDNs require.  The fetched XML bytes are then handed to feedparser
  for parsing.  This keeps credential/retry logic in one place.
* **feedparser soft dependency**: if feedparser is not installed, ``probe``
  returns ``SCHEMA_MISMATCH`` and ``pull`` yields nothing rather than crashing
  the pipeline.  Add ``feedparser>=6`` to requirements to enable this family.
* **Headlines only**: full article bodies would require an additional HTTP GET
  per entry.  This is flagged but deferred from v1 scope; the ``snippet``
  column receives the RSS ``<description>``/``<summary>`` field (which is
  often a short teaser, not the full body).
* **Idempotency**: no unique constraint exists on ``raw_news`` beyond the
  primary key.  The pipeline stage is expected to de-duplicate on
  ``(source_id, url)`` via ``ON CONFLICT DO NOTHING`` or a pre-check.
* **Since filter**: applied in-memory against ``published_at``.  RSS feeds
  expose only the most recent N items (typically 100–200) so a ``since`` value
  older than the feed's window silently yields everything available.

Raw table: ``raw_news`` (headline, url, published_at, entity, snippet).
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator
from urllib.parse import urlparse

from connectors.base import Connector, ProbeResult, classify_exception, classify_http_error
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.families.newswire_rss")

# ---- feedparser soft dependency ----------------------------------------
try:
    import feedparser as _feedparser  # type: ignore[import]
    _HAVE_FEEDPARSER = True
except ImportError:
    _feedparser = None  # type: ignore[assignment]
    _HAVE_FEEDPARSER = False
    logger.info(
        "feedparser not installed — NewswireRssFamilyConnector will degrade "
        "gracefully (probe → SCHEMA_MISMATCH, pull → empty). "
        "Add 'feedparser>=6' to requirements to enable."
    )

# ---- HTML stripping ----------------------------------------------------
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Maximum characters stored in the ``snippet`` column.
_MAX_SNIPPET = 500

# ---- Default feed URLs (stored in sources.url_pattern — listed here
#      as documentation; the connector reads from source_row at runtime) --
_DEFAULT_URLS: dict[str, str] = {
    "prnewswire_rss":   "https://www.prnewswire.com/rss/news-releases-list.rss",
    "businesswire_rss": "https://feed.businesswire.com/rss/home/?rss=G1",
    "globenewswire_rss": "https://www.globenewswire.com/RssFeed/subjectCode/12-Industrial",
}


# ---- Helpers -----------------------------------------------------------

def _strip_html(text: str | None) -> str | None:
    """Remove HTML markup from *text* and collapse whitespace (best-effort)."""
    if not text:
        return None
    cleaned = _HTML_TAG_RE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:_MAX_SNIPPET] or None


def _parse_struct_time(st: Any) -> datetime | None:
    """Convert a feedparser ``time.struct_time`` 9-tuple to a UTC datetime.

    feedparser always converts publish dates to UTC ``struct_time``
    (``entry.published_parsed``). ``calendar.timegm`` interprets the struct
    as UTC, producing a POSIX timestamp.
    """
    if st is None:
        return None
    try:
        ts = calendar.timegm(st)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_rfc2822(date_str: str | None) -> datetime | None:
    """Parse an RFC-2822 date string (typical RSS ``<pubDate>``) to UTC.

    Falls back to ``None`` on any parsing failure.
    """
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _parse_since(since: str | None) -> datetime | None:
    """Parse the pipeline's ISO ``since`` hint to a timezone-aware datetime."""
    if not since:
        return None
    clean = since.strip().replace("Z", "+00:00")
    try:
        if "T" in clean:
            dt = datetime.fromisoformat(clean)
        else:
            dt = datetime.fromisoformat(clean + "T00:00:00+00:00")
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _domain_from_url(url: str | None) -> str | None:
    """Extract the bare hostname from *url* (e.g. ``www.prnewswire.com``)."""
    if not url:
        return None
    try:
        return urlparse(url).netloc or None
    except Exception:  # noqa: BLE001
        return None


# ---- Base family connector ---------------------------------------------

class NewswireRssFamilyConnector(Connector):
    """Shared RSS ingestion logic for all newswire family connectors.

    Subclasses override ``source_id`` and register themselves under that
    name; everything else is inherited from this base.

    The feed URL is read at runtime from ``sources.url_pattern`` (populated
    by the seed YAML), so operators can swap or extend feeds without a code
    change.
    """

    raw_table = "raw_news"
    min_interval = 1.0  # Polite gap; RSS feeds are CDN-served but still rate-limit aggressive bots.

    def default_headers(self) -> dict[str, str]:
        """Identify ourselves as an RSS reader — some CDNs block generic agents."""
        return {
            "Accept": "application/rss+xml, application/atom+xml, application/xml, "
                      "text/xml;q=0.9, */*;q=0.8",
        }

    # ------------------------------------------------------------------ #
    # probe
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """Fetch the configured RSS feed and verify at least one entry is present.

        Returns ``SCHEMA_MISMATCH`` when feedparser is not installed or when
        the URL is unconfigured. Returns ``EMPTY`` on a successful HTTP fetch
        that yields no feed entries.
        """
        if not _HAVE_FEEDPARSER:
            return ProbeResult(
                "SCHEMA_MISMATCH",
                "feedparser not installed; add 'feedparser>=6' to requirements",
                None,
            )

        feed_url = self._feed_url()
        if not feed_url:
            return ProbeResult(
                "SCHEMA_MISMATCH",
                f"no url_pattern configured for source_id={self.source_id!r}",
                None,
            )

        try:
            resp = self.http.get(feed_url)
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

        feed = _feedparser.parse(resp.text)
        bozo: bool = bool(feed.get("bozo"))
        bozo_exc = feed.get("bozo_exception")
        entries: list[Any] = feed.get("entries") or []

        if bozo and not entries:
            return ProbeResult(
                "SCHEMA_MISMATCH",
                f"feedparser parse error ({type(bozo_exc).__name__}: {bozo_exc})",
                None,
            )
        if not entries:
            return ProbeResult("EMPTY", "feed is reachable but returned no entries", None)

        first = entries[0]
        sample = {
            "title": first.get("title"),
            "link": first.get("link"),
        }
        return ProbeResult(
            "OK",
            f"HTTP 200; {len(entries)} entries (bozo={bozo})",
            sample,
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
        """Fetch all entries from the configured RSS feed and yield verbatim records.

        RSS feeds expose only the most recent items (typically 100–200 per
        feed); there is no cursor-based pagination. The ``since`` hint filters
        entries by ``published_at`` in-memory so re-runs with the same
        ``since`` do not re-yield already-ingested articles.

        Each yielded dict carries the raw feedparser fields plus a
        ``_source_id`` tag, making :meth:`normalize` context-free.
        """
        if not _HAVE_FEEDPARSER:
            logger.warning(
                "%s: feedparser not installed — yielding nothing", self.source_id
            )
            return

        feed_url = self._feed_url()
        if not feed_url:
            logger.warning(
                "%s: no url_pattern configured — yielding nothing", self.source_id
            )
            return

        try:
            resp = self.http.get(feed_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: RSS fetch failed: %s", self.source_id, exc)
            return

        if resp.status_code != 200:
            logger.info(
                "%s: HTTP %s fetching feed — yielding nothing",
                self.source_id, resp.status_code,
            )
            return

        feed = _feedparser.parse(resp.text)
        entries: list[Any] = feed.get("entries") or []
        if not entries:
            logger.info("%s: RSS feed returned 0 entries", self.source_id)
            return

        since_dt = _parse_since(since)
        yielded = 0

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            published_raw = entry.get("published") or entry.get("updated") or None
            pub_dt = _parse_struct_time(published_struct) or _parse_rfc2822(published_raw)

            # Apply the since filter when both values are available and timezone-aware.
            if since_dt and pub_dt and pub_dt.tzinfo and pub_dt < since_dt:
                continue

            record: dict[str, Any] = {
                # Raw text fields (verbatim from feedparser).
                "title":            entry.get("title") or None,
                "link":             entry.get("link") or None,
                "published":        published_raw,
                # feedparser struct_time — not directly JSON-serialisable;
                # the pipeline writes raw_json via jsonb, which the pipeline's
                # json encoder converts using json.dumps(default=str).
                "published_parsed": list(published_struct) if published_struct else None,
                "summary":          entry.get("summary") or entry.get("description") or None,
                "tags": [
                    t.get("term")
                    for t in (entry.get("tags") or [])
                    if t.get("term")
                ],
                "authors": [
                    a.get("name")
                    for a in (entry.get("authors") or [])
                    if a.get("name")
                ],
                # Tag with source_id so normalize() can refer back if needed.
                "_source_id": self.source_id,
            }
            yield record
            yielded += 1

        logger.debug("%s: yielded %d/%d entries", self.source_id, yielded, len(entries))

    # ------------------------------------------------------------------ #
    # normalize
    # ------------------------------------------------------------------ #

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one verbatim RSS entry dict to typed ``raw_news`` columns.

        Parameters
        ----------
        raw:
            A dict as yielded by :meth:`pull` (containing feedparser fields).

        Returns
        -------
        dict
            ``headline``     — entry title (``raw["title"]``).
            ``url``          — canonical entry link (``raw["link"]``).
            ``published_at`` — UTC :class:`datetime` parsed from
              ``published_parsed`` (feedparser struct_time 9-list) or the raw
              RFC-2822 string ``published``; ``None`` when both are absent.
            ``entity``       — first RSS ``<category>``/tag term, or the URL
              domain (e.g. ``businesswire.com``) when no tags are present.
            ``snippet``      — HTML-stripped ``summary``/``description`` capped
              at 500 characters; ``None`` when absent.
        """
        if not raw:
            return {}

        headline: str | None = raw.get("title") or None
        url: str | None = raw.get("link") or None

        # Reconstitute struct_time from the serialised list (9 integers).
        published_struct_raw = raw.get("published_parsed")
        published_struct: Any = None
        if isinstance(published_struct_raw, (list, tuple)) and len(published_struct_raw) == 9:
            published_struct = tuple(published_struct_raw)

        published_at: datetime | None = (
            _parse_struct_time(published_struct)
            or _parse_rfc2822(raw.get("published") or None)
        )

        tags: list[str] = raw.get("tags") or []
        entity: str | None = tags[0] if tags else _domain_from_url(url)

        snippet: str | None = _strip_html(
            raw.get("summary") or raw.get("description") or None
        )

        return {
            "headline":     headline,
            "url":          url,
            "published_at": published_at,
            "entity":       entity,
            "snippet":      snippet,
        }

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _feed_url(self) -> str:
        """Return the configured RSS feed URL for this source.

        Priority: ``sources.url_pattern`` (runtime row) > class-level default.
        """
        url = (self.base_url or "").strip()
        if not url:
            url = _DEFAULT_URLS.get(self.source_id, "")
        return url


# ---- Concrete per-source registrations ---------------------------------

@register("prnewswire_rss")
class PrNewswireRssConnector(NewswireRssFamilyConnector):
    """PR Newswire RSS connector.

    Default feed: https://www.prnewswire.com/rss/news-releases-list.rss

    To target a topic-specific feed (e.g. electronics), set
    ``sources.url_pattern`` to the relevant RSS URL, e.g.:
    ``https://www.prnewswire.com/rss/electronics-news-releases-list.rss``
    """

    source_id = "prnewswire_rss"


@register("businesswire_rss")
class BusinessWireRssConnector(NewswireRssFamilyConnector):
    """Business Wire RSS connector.

    Default feed: https://feed.businesswire.com/rss/home/?rss=G1 (all industries).

    To narrow to a specific vertical, replace ``rss=G1`` with a topic code, e.g.
    ``rss=G17`` (technology) — see https://www.businesswire.com/html/home/20050808005951/en/
    """

    source_id = "businesswire_rss"


@register("globenewswire_rss")
class GlobeNewswireRssConnector(NewswireRssFamilyConnector):
    """GlobeNewswire RSS connector.

    Default feed: https://www.globenewswire.com/RssFeed/subjectCode/12-Industrial

    Override ``url_pattern`` for a keyword feed:
    ``https://www.globenewswire.com/RssFeed/keyword/power+components``
    """

    source_id = "globenewswire_rss"
