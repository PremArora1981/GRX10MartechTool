"""Scraping connector base — HTML scraping with a permanent low-confidence flag.

Some catalog sources expose no API (JEDEC member roster, SIA/SEMI mirrors, ACEA,
various national portals). A scraping connector is allowed, but the v1 spec is
explicit that scraped data is second-class: it carries **ToS risk** and can only
ever feed lower-confidence methods. To make that impossible to forget, this base
class *always* embeds a ToS-risk / low-confidence warning in its
:meth:`probe` detail and exposes :attr:`low_confidence = True`, regardless of
whether the fetch succeeded.

Subclasses implement :meth:`scrape` (turn a parsed page into verbatim record
dicts) and :meth:`normalize`; the base handles fetching, BeautifulSoup parsing,
the 7-state probe, and the ever-present risk flag.

BeautifulSoup (``bs4``) is imported lazily: if it is not installed the connector
degrades to ``UNREACHABLE`` on probe and yields nothing — it never fabricates
data.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import Any, Iterator, Mapping

from connectors.base import Connector, ProbeResult, classify_exception, classify_http_error

logger = logging.getLogger("grx10.connectors.scrape")

# Surfaced verbatim in every probe detail and in the Connectors admin UI so the
# operator always sees the risk before relying on scraped numbers.
TOS_RISK_NOTICE = (
    "LOW-CONFIDENCE / ToS-RISK: scraped source — verify terms of service; "
    "data may only feed Tier-B/C methods and can never qualify a cell HIGH."
)


def _load_bs4() -> Any | None:
    """Import BeautifulSoup lazily; return ``None`` (logged) if unavailable."""
    try:
        from bs4 import BeautifulSoup  # type: ignore

        return BeautifulSoup
    except Exception as exc:  # noqa: BLE001
        logger.warning("beautifulsoup4 (bs4) not installed: %s", exc)
        return None


class ScrapeConnector(Connector):
    """Base class for HTML-scraping connectors. Always flagged low-confidence.

    Subclasses set :attr:`probe_path` (a cheap page to fetch for the health
    probe) and implement :meth:`scrape` + :meth:`normalize`.
    """

    #: Permanent marker: scraped data is structurally low-confidence (read by
    #: tooling / methods that must cap confidence for scrape-sourced rows).
    low_confidence: bool = True
    #: A lightweight page used by :meth:`probe` to test reachability.
    probe_path: str = ""
    #: Default HTML parser for BeautifulSoup (lxml optional; html.parser is stdlib).
    parser: str = "html.parser"

    def default_headers(self) -> dict[str, str]:
        # Browser-like Accept improves success against bot-protected pages; the
        # descriptive User-Agent is still set by the shared HttpClient.
        return {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

    # ------------------------------------------------------------------ #
    # Fetch + parse
    # ------------------------------------------------------------------ #
    def fetch_soup(self, path: str = "", **kwargs: Any) -> Any | None:
        """Fetch ``path`` and return a parsed BeautifulSoup tree (or ``None``)."""
        bs = _load_bs4()
        if bs is None:
            return None
        resp = self.http.get(path, **kwargs)
        if resp.status_code >= 300:
            logger.info("%s scrape fetch %s -> HTTP %s",
                        self.source_id, path or self.base_url, resp.status_code)
            return None
        return bs(resp.text, self.parser)

    # ------------------------------------------------------------------ #
    # probe — ALWAYS carries the ToS / low-confidence notice
    # ------------------------------------------------------------------ #
    def probe(self) -> ProbeResult:
        """Reachability probe whose detail ALWAYS includes the ToS-risk notice."""
        bs = _load_bs4()
        if bs is None:
            return ProbeResult(
                "UNREACHABLE",
                f"{TOS_RISK_NOTICE} | beautifulsoup4 not installed",
                None,
            )
        try:
            resp = self.http.get(self.probe_path)
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            return ProbeResult(status, f"{TOS_RISK_NOTICE} | {detail}", None)

        if resp.status_code >= 300:
            status = classify_http_error(resp.status_code, resp.text)
            return ProbeResult(
                status,
                f"{TOS_RISK_NOTICE} | HTTP {resp.status_code}",
                None,
            )
        # Reachable. Still flag the risk and downgrade an empty page to EMPTY.
        text = (resp.text or "").strip()
        if not text:
            return ProbeResult("EMPTY", f"{TOS_RISK_NOTICE} | page was empty", None)
        sample = bs(text, self.parser).get_text(" ", strip=True)[:300]
        return ProbeResult("OK", f"{TOS_RISK_NOTICE} | HTTP 200", sample)

    # ------------------------------------------------------------------ #
    # pull — drives scrape(); yields verbatim record dicts
    # ------------------------------------------------------------------ #
    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Fetch + parse the configured page(s) and yield verbatim record dicts."""
        if _load_bs4() is None:
            return
        try:
            yield from self.scrape(taxonomy=taxonomy, geographies=geographies, since=since)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s scrape failed: %s", self.source_id, exc)
            return

    # ------------------------------------------------------------------ #
    # Subclass contract
    # ------------------------------------------------------------------ #
    @abstractmethod
    def scrape(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield verbatim record dicts parsed from the scraped page(s).

        Use :meth:`fetch_soup` to obtain parsed trees. Each yielded dict is
        stored verbatim as the raw payload; :meth:`normalize` types it later.
        """
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one scraped record to typed columns of :attr:`raw_table`."""
        raise NotImplementedError
