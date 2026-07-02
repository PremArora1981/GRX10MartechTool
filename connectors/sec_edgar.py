"""SEC EDGAR connector.

Pulls consolidated financial data (us-gaap / ifrs-full ``companyfacts``) from
``data.sec.gov`` into ``raw_filings``.  A companion class,
:class:`SecEdgar8KConnector`, discovers 8-K events (M&A, material agreements,
earnings disclosures) and yields them as ``raw_news``-style payloads for
catalyst ingestion.

Endpoints used
--------------
* ``GET https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json``
  Clean consolidated facts JSON — no per-filing parsing overhead.
* ``GET https://data.sec.gov/submissions/CIK{cik:010d}.json``
  Company submission history (filings, 8-K item strings, dates).
* ``GET https://www.sec.gov/files/company_tickers.json``
  CIK ↔ ticker ↔ company-name index (downloaded once per instance).

Rate limit
----------
SEC EDGAR enforces a **10 req/s** ceiling for all automated clients.  This
connector uses ``min_interval = 0.101`` s (≈ 9.9 req/s) to stay comfortably
within that limit.

User-Agent — MANDATORY
----------------------
SEC EDGAR requires a descriptive User-Agent that identifies the requester by
name and email address (see EDGAR developer FAQ).  Anonymous or missing UAs
receive 403 responses.  The ``GRX10_USER_AGENT`` environment variable (default
set in ``connectors/http.py``) satisfies this requirement.  Its value MUST
follow the pattern::

    CompanyName/Version (contact@email.com)

or::

    CompanyName research@email.com

Company targeting
-----------------
The connector pulls only a *configured* set of companies — it does not scan all
600k+ EDGAR filers.  Provide targets via either (both are combined):

1. **``source_row["notes"]``** as a JSON string — highest priority::

       {"cik_list": ["0000320193", "0001018724"],
        "ticker_list": ["MSFT", "INTC"]}

2. **Environment variables** ``EDGAR_CIK_LIST`` and/or ``EDGAR_TICKER_LIST``
   (comma-separated).

If neither is configured, :meth:`~SecEdgarConnector.pull` yields nothing (the
source is EMPTY, not an error).

Segment facts — EXTENSION POINT
--------------------------------
The ``companyfacts`` JSON endpoint returns **consolidated** (entity-level)
revenue totals only.  Dimensional segment data (product-line, geographic-
segment, customer-group revenue split) is embedded in the inline-XBRL instance
documents and is NOT extracted in v1.  See
:func:`_xbrl_segment_extension_point` for the full implementation roadmap.

8-K discovery
-------------
:class:`SecEdgar8KConnector` (registered as ``"sec_edgar_8k"``) fetches
``submissions/CIK{cik}.json`` for the same companies and yields one dict per
8-K filing.  Normalised rows match the ``raw_news`` schema columns
(``headline``, ``url``, ``published_at``, ``entity``, ``snippet``).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Any, Iterator

from connectors.base import (
    Connector,
    ProbeResult,
    classify_exception,
    classify_http_error,
)
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.sec_edgar")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DATA_BASE = "https://data.sec.gov"
_WWW_BASE = "https://www.sec.gov"
_COMPANY_TICKERS_URL = f"{_WWW_BASE}/files/company_tickers.json"

# 10 req/s EDGAR ceiling → 0.101 s gives ≈ 9.9 req/s.
_MIN_INTERVAL: float = 0.101

# Stable well-known CIK used for the probe health-check (Apple Inc.).
_PROBE_CIK: str = "0000320193"

# Revenue concepts tried in descending priority.  The FIRST concept found with
# annual data for a given company is used; subsequent concepts are skipped to
# prevent double-counting (companies rarely report under more than one name).
_REVENUE_CONCEPTS: list[tuple[str, str]] = [
    ("us-gaap", "Revenues"),
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "SalesRevenueNet"),
    ("us-gaap", "SalesRevenueGoodsNet"),
    ("us-gaap", "NetRevenues"),
    ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    ("ifrs-full", "Revenue"),
    ("ifrs-full", "RevenueFromSaleOfGoods"),
]

# Annual SEC form types included for revenue sizing (quarterly 10-Q excluded).
_ANNUAL_FORMS: frozenset[str] = frozenset({"10-K", "20-F", "10-KT", "20-FT"})

# 8-K item codes treated as material catalyst events for the news connector.
_CATALYST_ITEMS: frozenset[str] = frozenset({
    "1.01",  # Entry into a Material Definitive Agreement
    "1.02",  # Termination of a Material Definitive Agreement
    "2.01",  # Completion of Acquisition or Disposition of Assets
    "2.03",  # Creation of a Direct Financial Obligation
    "5.02",  # Departure / Appointment of Directors or Officers
    "8.01",  # Other Events (catch-all for material disclosures)
})

# Human-readable labels for 8-K item codes.
_ITEM_LABELS: dict[str, str] = {
    "1.01": "Material Agreement",
    "1.02": "Agreement Termination",
    "2.01": "Acquisition / Disposition",
    "2.02": "Financial Results",
    "2.03": "Financial Obligation",
    "5.02": "Director / Officer Change",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Material Event",
}


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _zero_pad_cik(cik: str | int) -> str:
    """Return a 10-digit zero-padded CIK string (SEC archive format).

    Examples
    --------
    >>> _zero_pad_cik("320193")
    '0000320193'
    >>> _zero_pad_cik(1018724)
    '0001018724'
    """
    try:
        return str(int(str(cik).strip().lstrip("0") or "0")).zfill(10)
    except (ValueError, TypeError):
        return "0000000000"


def _accn_no_hyphens(accession_number: str) -> str:
    """Strip hyphens from an accession number for EDGAR archive URL paths."""
    return accession_number.replace("-", "")


def _filing_index_url(cik: str, accession_number: str) -> str:
    """Return the EDGAR filing index URL for a CIK + accession number."""
    return (
        f"{_WWW_BASE}/Archives/edgar/data/"
        f"{int(cik)}/{_accn_no_hyphens(accession_number)}/"
    )


def _filing_doc_url(cik: str, accession_number: str, primary_doc: str) -> str:
    """Return the URL of the primary document within an EDGAR filing."""
    return (
        f"{_WWW_BASE}/Archives/edgar/data/"
        f"{int(cik)}/{_accn_no_hyphens(accession_number)}/{primary_doc}"
    )


def _parse_since_date(since: str | None) -> date | None:
    """Parse an ISO-date string (YYYY-MM-DD) into a :class:`date`, or None."""
    if not since:
        return None
    try:
        return date.fromisoformat(since[:10])
    except (ValueError, TypeError):
        return None


def _filed_on_or_after(filed_str: str, cutoff: date) -> bool:
    """Return True when ``filed_str`` represents a date >= ``cutoff``.

    Unknown / unparseable dates are treated as in-scope (True) to avoid
    silently dropping data.
    """
    if not filed_str:
        return True
    try:
        return date.fromisoformat(filed_str[:10]) >= cutoff
    except (ValueError, TypeError):
        return True


def _parse_notes_config(notes: str | None) -> tuple[list[str], list[str]]:
    """Extract ``cik_list`` and ``ticker_list`` from a JSON-encoded notes field.

    Expected ``source_row["notes"]`` format::

        {"cik_list": ["0000320193", "0001018724"], "ticker_list": ["MSFT"]}

    Both keys are optional.  Non-JSON or empty values return ([], []).
    """
    if not notes:
        return [], []
    try:
        cfg = json.loads(notes)
        ciks = [str(c).strip() for c in cfg.get("cik_list", []) if str(c).strip()]
        tickers = [
            str(t).strip().upper()
            for t in cfg.get("ticker_list", [])
            if str(t).strip()
        ]
        return ciks, tickers
    except (json.JSONDecodeError, TypeError, AttributeError):
        logger.debug("EDGAR: source_row.notes is not valid JSON — ignoring")
        return [], []


def _parse_env_config() -> tuple[list[str], list[str]]:
    """Read ``EDGAR_CIK_LIST`` / ``EDGAR_TICKER_LIST`` from the environment.

    Both variables are comma-separated.  Returns (ciks, tickers).
    """
    cik_raw = os.environ.get("EDGAR_CIK_LIST", "")
    ticker_raw = os.environ.get("EDGAR_TICKER_LIST", "")
    ciks = [c.strip() for c in cik_raw.split(",") if c.strip()]
    tickers = [t.strip().upper() for t in ticker_raw.split(",") if t.strip()]
    return ciks, tickers


def _xbrl_segment_extension_point(
    cik: str,
    accession_number: str,
) -> list[dict[str, Any]]:
    """EXTENSION POINT — dimensional segment fact extraction (NOT implemented in v1).

    The ``companyfacts`` JSON endpoint delivers only **consolidated**
    (entity-level) revenue totals.  Dimensional segment data — product-line
    revenue, geographic-segment split, customer-group breakdown — is encoded
    inside the inline-XBRL instance documents filed with each 10-K / 20-F and
    is NOT extracted here.

    Implementation roadmap for a future version
    -------------------------------------------
    1. **Resolve the iXBRL document URL.**
       From ``data.sec.gov/submissions/CIK{cik}.json`` locate the most recent
       10-K / 20-F accession.  Walk ``filings.recent.primaryDocument`` to find
       the ``.htm`` iXBRL file.

    2. **Fetch and parse the iXBRL file.**
       Use an inline-XBRL library — ``arelle`` (full spec support) or the
       SEC's own open-source ``ixbrl-viewer`` extractor — to deserialise the
       document into a set of XBRL facts with dimensional context.

    3. **Walk segment axes.**
       The two most common axes are:

       * ``us-gaap:StatementBusinessSegmentsAxis`` → product / business segment
         members (e.g. ``AppleMac``, ``AppleiPhone``)
       * ``us-gaap:StatementGeographicalAxis`` → geography members
         (e.g. ``AmericasMember``, ``AsiaPacificMember``)

       For each (axis, member) pair, extract the associated ``Revenues`` fact
       value and its period context.

    4. **Alternative fast-path — EDGAR bulk XBRL data sets.**
       The quarterly ZIP at
       ``https://www.sec.gov/dera/data/financial-statements-data-sets``
       contains ``num.txt`` with dimensional context codes.  A local copy
       (~several GB / quarter) enables batch extraction without per-filing
       HTTP requests — recommended when coverage > 50 companies.

    Until this extension is implemented, the ``segment`` and ``geography``
    columns are written as ``None`` in every ``raw_filings`` row produced by
    this connector.

    Parameters
    ----------
    cik:
        Zero-padded 10-digit CIK string.
    accession_number:
        Accession number string (hyphens optional).

    Returns
    -------
    list[dict]
        Segment fact dicts matching ``raw_filings`` typed columns.  Always
        empty in v1 — never fabricates data.
    """
    # v1: no-op.  Do not fabricate segment rows.
    logger.debug(
        "EDGAR _xbrl_segment_extension_point called for CIK %s accn %s "
        "— not implemented in v1; returning []",
        cik,
        accession_number,
    )
    return []


# ---------------------------------------------------------------------------
# Main financial-filings connector
# ---------------------------------------------------------------------------

@register("sec_edgar")
class SecEdgarConnector(Connector):
    """SEC EDGAR connector — consolidated financial data → ``raw_filings``.

    Fetches annual revenue facts (10-K / 20-F) from the EDGAR
    ``companyfacts`` JSON endpoint for a configured set of companies
    identified by CIK or ticker.

    The connector is unauthenticated (SEC EDGAR requires no API key) but a
    **descriptive User-Agent is mandatory** per SEC policy.  The
    ``GRX10_USER_AGENT`` environment variable (initialised in
    ``connectors/http.py``) satisfies this requirement.

    Segment facts
    -------------
    Only consolidated revenue is extracted.  Dimensional segment breakdown
    (product line, geography) requires raw-XBRL parsing; see
    :func:`_xbrl_segment_extension_point`.

    8-K helper
    ----------
    :meth:`pull_8k` and :meth:`normalize_8k` are also exposed for use by
    :class:`SecEdgar8KConnector` and any caller that needs 8-K event data
    without going through the filings pipeline.
    """

    source_id: str = "sec_edgar"
    raw_table: str = "raw_filings"
    min_interval: float = _MIN_INTERVAL

    # ------------------------------------------------------------------ #
    # Connector contract
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """Verify EDGAR reachability using a stable well-known CIK (Apple Inc.).

        Fetches the companyfacts JSON for CIK 0000320193.  A 200 response with
        valid JSON confirms both endpoint reachability and User-Agent acceptance.
        No company-targeting configuration is required for the probe.
        """
        url = f"{_DATA_BASE}/api/xbrl/companyfacts/CIK{_PROBE_CIK}.json"
        try:
            resp = self.http.get(url)
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail)

        if resp.status_code == 200:
            try:
                data = resp.json()
                entity = data.get("entityName", "?")
                taxonomies = list(data.get("facts", {}).keys())
                return ProbeResult(
                    "OK",
                    (
                        f"EDGAR reachable; probe entity={entity!r}; "
                        f"taxonomies={taxonomies}"
                    ),
                    {"entityName": entity, "cik": _PROBE_CIK, "taxonomies": taxonomies},
                )
            except Exception as exc:  # noqa: BLE001
                return ProbeResult(
                    "SCHEMA_MISMATCH",
                    f"EDGAR returned HTTP 200 but JSON parse failed: {exc}",
                )

        status = classify_http_error(resp.status_code, resp.text)
        return ProbeResult(
            status,
            f"EDGAR probe HTTP {resp.status_code}: {resp.text[:300]}",
        )

    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield verbatim companyfacts fact dicts for configured companies.

        Each yielded dict represents one (company, fiscal-period,
        revenue-concept) annual fact from EDGAR — stored as ``raw_json`` in
        ``raw_filings`` and mapped to typed columns via :meth:`normalize`.

        **Concept selection:** only the highest-priority revenue concept in
        :data:`_REVENUE_CONCEPTS` that has annual data is used per company,
        preventing double-counting when a company reports under multiple
        concept names.

        **Annual forms only:** facts with ``form`` in
        :data:`_ANNUAL_FORMS` (10-K, 20-F, 10-KT, 20-FT) are included.

        Parameters
        ----------
        taxonomy:
            Active ``taxonomy_subcategories`` rows (used for log context; this
            connector does not filter by HS code — all configured companies
            are pulled regardless of subcategory).
        geographies:
            Active ``geographies`` rows (not used for filtering here).
        since:
            ISO-date lookback hint (YYYY-MM-DD).  Only filings with ``filed``
            on or after this date are yielded.  Pass ``None`` for full history.
        """
        targets = self._resolve_targets()
        if not targets:
            logger.warning(
                "EDGAR pull: no CIK / ticker targets configured. "
                "Provide targets via source_row.notes JSON (keys: cik_list, "
                "ticker_list) or env vars EDGAR_CIK_LIST / EDGAR_TICKER_LIST."
            )
            return

        since_date = _parse_since_date(since)
        logger.info(
            "EDGAR pull: %d target companies, since=%s", len(targets), since
        )

        for cik, ticker, name in targets:
            logger.debug("EDGAR: fetching companyfacts CIK=%s (%s)", cik, name)
            facts_data = self._fetch_companyfacts(cik)
            if facts_data is None:
                continue  # error already logged in _fetch_companyfacts
            yield from self._extract_revenue_facts(
                facts_data, cik, ticker, name, since_date
            )

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map a companyfacts fact dict to ``raw_filings`` typed columns.

        Parameters
        ----------
        raw:
            A dict as yielded by :meth:`pull`.  Keys prefixed with ``_`` are
            connector metadata; the remaining keys are verbatim EDGAR fields
            (``end``, ``val``, ``accn``, ``fy``, ``fp``, ``form``, ``filed``).

        Returns
        -------
        dict
            Subset of ``raw_filings`` columns.  ``segment`` and ``geography``
            are always ``None`` — see :func:`_xbrl_segment_extension_point`.
        """
        fp: str = str(raw.get("fp") or "")
        fy: Any = raw.get("fy")
        # Build a period string like "FY2023" or "Q12023".
        period: str | None = (
            f"{fp}{fy}" if (fp or fy) else (raw.get("end") or None)
        )

        cik = raw.get("_cik", "")
        accn = raw.get("accn", "")
        doc_url = _filing_index_url(cik, accn) if (cik and accn) else None

        val = raw.get("val")
        revenue_usd: float | None = float(val) if val is not None else None

        return {
            "filer": raw.get("_entity_name"),
            "ticker": raw.get("_ticker") or None,
            "period": period,
            # Consolidated totals only — no segment / geography in v1.
            "segment": None,
            "geography": None,
            "revenue_usd": revenue_usd,  # raw USD (not millions); methods convert
            "doc_url": doc_url,
        }

    # ------------------------------------------------------------------ #
    # 8-K helper — also consumed by SecEdgar8KConnector
    # ------------------------------------------------------------------ #

    def pull_8k(
        self,
        cik: str,
        name: str,
        ticker: str,
        since_date: date | None = None,
        *,
        catalyst_only: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Discover 8-K filings for one company and yield raw_news-style dicts.

        Fetches ``data.sec.gov/submissions/CIK{cik}.json`` and filters for
        ``form == "8-K"`` entries.  Each yielded dict contains verbatim EDGAR
        submission fields plus derived ``raw_news`` keys (``_headline``,
        ``_url``, ``_snippet``).

        Note: only the ``recent`` filing batch (~1 000 most-recent filings) is
        processed.  Older 8-Ks are listed in paginated ``files`` entries within
        the submissions JSON; that pagination is not followed in v1.

        Parameters
        ----------
        cik:
            Zero-padded 10-digit CIK string.
        name:
            Company name (used when submissions JSON is unavailable).
        ticker:
            Primary ticker symbol (may be empty string).
        since_date:
            Lookback cutoff — only 8-Ks filed on or after this date.
        catalyst_only:
            When ``True``, only yield 8-Ks whose item codes intersect
            :data:`_CATALYST_ITEMS` (M&A, agreements, acquisitions).
        """
        url = f"{_DATA_BASE}/submissions/CIK{cik}.json"
        try:
            resp = self.http.get(url)
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            logger.warning(
                "EDGAR submissions error for CIK %s: %s — %s", cik, status, detail
            )
            return

        if resp.status_code != 200:
            status = classify_http_error(resp.status_code, resp.text)
            logger.warning(
                "EDGAR submissions HTTP %d for CIK %s (%s)",
                resp.status_code,
                cik,
                status,
            )
            return

        try:
            sub = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EDGAR submissions JSON parse error for CIK %s: %s", cik, exc
            )
            return

        entity_name: str = sub.get("name") or name
        recent: dict[str, list] = sub.get("filings", {}).get("recent", {})

        accn_list: list[str] = recent.get("accessionNumber", [])
        form_list: list[str] = recent.get("form", [])
        date_list: list[str] = recent.get("filingDate", [])
        doc_list: list[str] = recent.get("primaryDocument", [])
        items_list: list[str] = recent.get("items", [])

        for i, accn in enumerate(accn_list):
            form = form_list[i] if i < len(form_list) else ""
            if form != "8-K":
                continue

            filing_date_str = date_list[i] if i < len(date_list) else ""
            if since_date and filing_date_str:
                if not _filed_on_or_after(filing_date_str, since_date):
                    continue

            items_str: str = items_list[i] if i < len(items_list) else ""
            primary_doc: str = doc_list[i] if i < len(doc_list) else ""

            item_codes: set[str] = {
                c.strip() for c in items_str.split(",") if c.strip()
            }
            is_catalyst = bool(item_codes & _CATALYST_ITEMS)

            if catalyst_only and not is_catalyst:
                continue

            item_labels = [
                _ITEM_LABELS.get(c, c) for c in sorted(item_codes) if c
            ]
            items_human = "; ".join(item_labels) if item_labels else "8-K filing"

            doc_url = (
                _filing_doc_url(cik, accn, primary_doc)
                if primary_doc
                else _filing_index_url(cik, accn)
            )

            yield {
                # Connector metadata (for normalize_8k to consume)
                "_source": "sec_edgar_8k",
                "_cik": cik,
                "_ticker": ticker,
                "_entity_name": entity_name,
                "_form": "8-K",
                "_items": items_str,
                "_item_codes": sorted(item_codes),
                "_is_catalyst": is_catalyst,
                # Verbatim EDGAR submission entry fields
                "accessionNumber": accn,
                "filingDate": filing_date_str,
                "primaryDocument": primary_doc,
                "items": items_str,
                # Pre-computed raw_news columns (also used by normalize_8k)
                "_headline": f"{entity_name} — 8-K: {items_human}",
                "_url": doc_url,
                "_snippet": (
                    f"SEC Form 8-K filed {filing_date_str} by "
                    f"{entity_name} (CIK {cik}). "
                    f"Items: {items_str or 'N/A'}. "
                    f"{'Material catalyst detected.' if is_catalyst else ''}"
                ).strip(),
            }

    def normalize_8k(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map a :meth:`pull_8k` payload to ``raw_news`` typed columns.

        Parameters
        ----------
        raw:
            A dict as yielded by :meth:`pull_8k`.

        Returns
        -------
        dict
            Keys matching ``raw_news``: ``headline``, ``url``,
            ``published_at``, ``entity``, ``snippet``.
        """
        filing_date = raw.get("filingDate") or ""
        # Store as a timezone-aware ISO string for the TIMESTAMPTZ column.
        published_at: str | None = (
            f"{filing_date}T00:00:00+00:00" if filing_date else None
        )

        return {
            "headline": raw.get("_headline"),
            "url": raw.get("_url"),
            "published_at": published_at,
            "entity": raw.get("_ticker") or raw.get("_entity_name"),
            "snippet": raw.get("_snippet"),
        }

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _get_company_tickers(self) -> dict[str, dict[str, str]]:
        """Download and cache the EDGAR company ticker → CIK index.

        Fetches ``https://www.sec.gov/files/company_tickers.json`` once per
        connector instance.  Returns a dict keyed by uppercase ticker with
        values ``{"cik_str": "0000320193", "title": "Apple Inc."}``.
        Returns ``{}`` on any fetch or parse failure.
        """
        if hasattr(self, "_tickers_cache"):
            return self._tickers_cache  # type: ignore[attr-defined]

        tickers_map: dict[str, dict[str, str]] = {}
        try:
            resp = self.http.get(_COMPANY_TICKERS_URL)
            if resp.status_code == 200:
                data = resp.json()
                # Response format: {"0": {"cik_str": "320193", "ticker": "AAPL",
                #                         "title": "Apple Inc."}, "1": {...}, ...}
                for entry in data.values():
                    ticker = str(entry.get("ticker", "")).upper().strip()
                    cik_str = _zero_pad_cik(entry.get("cik_str", "0"))
                    title = str(entry.get("title", ""))
                    if ticker:
                        tickers_map[ticker] = {"cik_str": cik_str, "title": title}
                logger.debug(
                    "EDGAR company_tickers: loaded %d ticker entries", len(tickers_map)
                )
            else:
                logger.warning(
                    "EDGAR company_tickers HTTP %d — ticker → CIK resolution "
                    "will be unavailable",
                    resp.status_code,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EDGAR company_tickers unavailable: %s — ticker targets skipped",
                exc,
            )

        self._tickers_cache: dict[str, dict[str, str]] = tickers_map
        return tickers_map

    def _resolve_targets(self) -> list[tuple[str, str, str]]:
        """Resolve the configured company targets to (cik, ticker, name) tuples.

        Merges targets from ``source_row["notes"]`` JSON and environment
        variables.  Direct CIKs are resolved to name + ticker via the
        submissions endpoint; ticker strings are resolved via
        ``company_tickers.json``.

        Duplicate CIKs (regardless of which config path introduced them) are
        deduplicated.  Returns an empty list when nothing is configured.
        """
        ciks_notes, tickers_notes = _parse_notes_config(
            self.source_row.get("notes")
        )
        ciks_env, tickers_env = _parse_env_config()

        # Preserve insertion order, deduplicate.
        all_ciks: list[str] = list(dict.fromkeys(ciks_notes + ciks_env))
        all_tickers: list[str] = list(dict.fromkeys(tickers_notes + tickers_env))

        if not all_ciks and not all_tickers:
            return []

        targets: list[tuple[str, str, str]] = []
        seen_ciks: set[str] = set()

        # --- Direct CIKs: fetch submissions JSON to get name + primary ticker ---
        for raw_cik in all_ciks:
            padded = _zero_pad_cik(raw_cik)
            if padded in seen_ciks:
                continue
            seen_ciks.add(padded)

            url = f"{_DATA_BASE}/submissions/CIK{padded}.json"
            try:
                resp = self.http.get(url)
            except Exception as exc:  # noqa: BLE001
                status, detail = classify_exception(exc)
                logger.warning(
                    "EDGAR submissions error for CIK %s: %s — skipping",
                    padded,
                    detail,
                )
                continue

            if resp.status_code != 200:
                logger.warning(
                    "EDGAR submissions HTTP %d for CIK %s — skipping",
                    resp.status_code,
                    padded,
                )
                continue

            try:
                sub = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EDGAR submissions JSON error for CIK %s: %s", padded, exc
                )
                continue

            name: str = sub.get("name") or padded
            primary_tickers: list[str] = sub.get("tickers") or []
            ticker: str = primary_tickers[0] if primary_tickers else ""
            targets.append((padded, ticker, name))

        # --- Ticker strings: resolve via company_tickers.json ---
        if all_tickers:
            tickers_map = self._get_company_tickers()
            for t in all_tickers:
                info = tickers_map.get(t)
                if not info:
                    logger.warning(
                        "EDGAR: ticker %r not found in company_tickers.json — skipping",
                        t,
                    )
                    continue
                cik = info["cik_str"]
                if cik in seen_ciks:
                    continue  # already added via direct CIK path
                seen_ciks.add(cik)
                name = info.get("title") or t
                targets.append((cik, t, name))

        logger.info("EDGAR: resolved %d unique company targets", len(targets))
        return targets

    def _fetch_companyfacts(self, cik: str) -> dict[str, Any] | None:
        """Fetch the companyfacts JSON for one CIK.

        Returns the parsed dict on success, or ``None`` on any error (error is
        logged at WARNING level; the caller skips to the next CIK).
        """
        url = f"{_DATA_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
        try:
            resp = self.http.get(url)
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            logger.warning(
                "EDGAR companyfacts error CIK %s: %s — %s", cik, status, detail
            )
            return None

        if resp.status_code != 200:
            status = classify_http_error(resp.status_code, resp.text)
            logger.warning(
                "EDGAR companyfacts HTTP %d for CIK %s (%s)",
                resp.status_code,
                cik,
                status,
            )
            return None

        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EDGAR companyfacts JSON parse error for CIK %s: %s", cik, exc
            )
            return None

    def _extract_revenue_facts(
        self,
        facts_data: dict[str, Any],
        cik: str,
        ticker: str,
        name: str,
        since_date: date | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield annual revenue fact dicts from a parsed companyfacts response.

        Iterates :data:`_REVENUE_CONCEPTS` in priority order and stops at the
        first concept that has annual (10-K / 20-F) data for this company —
        this prevents double-counting.  Only facts where the ``filed`` date
        is on or after ``since_date`` are yielded.

        Each yielded dict is the verbatim EDGAR fact entry enriched with
        connector metadata so :meth:`normalize` can reconstruct typed columns
        without an additional network call.
        """
        entity_name: str = facts_data.get("entityName") or name
        all_facts: dict[str, Any] = facts_data.get("facts", {})

        for taxonomy, concept in _REVENUE_CONCEPTS:
            usd_entries: list[dict[str, Any]] = (
                all_facts
                .get(taxonomy, {})
                .get(concept, {})
                .get("units", {})
                .get("USD", [])
            )
            if not usd_entries:
                continue

            # Keep only annual report forms.
            annual = [e for e in usd_entries if e.get("form") in _ANNUAL_FORMS]
            if not annual:
                continue

            # Apply lookback filter.
            if since_date:
                annual = [
                    e for e in annual
                    if _filed_on_or_after(e.get("filed", ""), since_date)
                ]
            if not annual:
                continue

            logger.debug(
                "EDGAR CIK=%s: concept %s/%s → %d annual facts",
                cik,
                taxonomy,
                concept,
                len(annual),
            )

            for fact in annual:
                yield {
                    # Connector metadata consumed by normalize()
                    "_source": "sec_edgar",
                    "_cik": cik,
                    "_ticker": ticker,
                    "_entity_name": entity_name,
                    "_taxonomy": taxonomy,
                    "_concept": concept,
                    # Verbatim EDGAR companyfacts fact entry
                    **fact,
                }

            # Stop after the first concept with data — do not double-count.
            return

        logger.debug(
            "EDGAR CIK=%s (%s): no matching revenue concept in companyfacts",
            cik,
            entity_name,
        )


# ---------------------------------------------------------------------------
# 8-K / catalyst-event connector
# ---------------------------------------------------------------------------

@register("sec_edgar_8k")
class SecEdgar8KConnector(Connector):
    """SEC EDGAR 8-K event connector → ``raw_news`` payloads.

    Discovers 8-K (Material Event) filings for the same configured companies
    as :class:`SecEdgarConnector` and yields one dict per 8-K filing.
    Rows are normalised to the ``raw_news`` schema (``headline``, ``url``,
    ``published_at``, ``entity``, ``snippet``).

    Useful for:

    * Detecting M&A activity (Items 1.01 / 2.01)
    * Earnings surprises (Item 2.02)
    * Material supplier / customer agreement events (Item 1.01)
    * Catalyst ingestion into ``catalysts`` via the ``news_event_detection``
      method.

    Configuration is identical to ``SecEdgarConnector`` (same
    ``source_row["notes"]`` JSON or env vars).  Configure the ``sources``
    seed row with::

        connector = "sec_edgar_8k"
        raw_table  = "raw_news"

    Note
    ----
    Only the ``recent`` submissions batch is scanned (~1 000 most-recent
    filings per company).  Older 8-Ks stored in paginated ``files`` entries
    in the submissions JSON are not followed in v1.
    """

    source_id: str = "sec_edgar_8k"
    raw_table: str = "raw_news"
    min_interval: float = _MIN_INTERVAL

    @property
    def _edgar(self) -> SecEdgarConnector:
        """Return a :class:`SecEdgarConnector` delegating to this instance's config.

        The delegate is built lazily and cached.  It uses its own throttled
        HTTP client initialised with the same ``min_interval`` so the 10 req/s
        ceiling is respected independently of whether both connectors run in
        the same pipeline session.
        """
        try:
            return self._edgar_delegate  # type: ignore[attr-defined]
        except AttributeError:
            self._edgar_delegate = SecEdgarConnector(
                self.source_row, self.credential
            )
            return self._edgar_delegate

    def probe(self) -> ProbeResult:
        """Delegate the health probe to :class:`SecEdgarConnector`."""
        return self._edgar.probe()

    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield 8-K filing dicts for all configured companies.

        Each dict contains verbatim EDGAR submission fields plus pre-computed
        ``raw_news`` fields.  Pass each dict through :meth:`normalize` to
        obtain the final typed column set for storage in ``raw_news``.

        Parameters
        ----------
        taxonomy:
            Active taxonomy rows (log context only).
        geographies:
            Active geography rows (not used for filtering).
        since:
            ISO-date lookback hint — only 8-Ks filed on or after this date.
        """
        since_date = _parse_since_date(since)
        edgar = self._edgar
        targets = edgar._resolve_targets()

        if not targets:
            logger.warning(
                "EDGAR 8-K pull: no targets configured. "
                "Provide targets via source_row.notes JSON or env vars."
            )
            return

        logger.info(
            "EDGAR 8-K pull: %d companies, since=%s", len(targets), since
        )
        for cik, ticker, name in targets:
            logger.debug("EDGAR 8-K: CIK=%s (%s)", cik, name)
            yield from edgar.pull_8k(cik, name, ticker, since_date)

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map a :meth:`~SecEdgarConnector.pull_8k` payload to ``raw_news`` columns.

        Delegates to :meth:`SecEdgarConnector.normalize_8k`.
        """
        return self._edgar.normalize_8k(raw)
