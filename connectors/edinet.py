"""EDINET v2 connector — Japan FSA structured annual-report filings (raw_filings).

Source ID : ``edinet``
Raw table : ``raw_filings``
Base URL  : https://api.edinet-fsa.go.jp/api/v2
Auth      : Subscription-Key (free; register at
            https://disclosure.edinet-fsa.go.jp/webapiregist/presend)

Phase-1 reference-engagement targets (from config/players.csv):
  * Murata Manufacturing Co., Ltd.  — EDINET E01150, ticker 6981
  * TDK Corporation                 — EDINET E01164, ticker 6762
  * Taiyo Yuden Co., Ltd.           — EDINET E01167, ticker 6976

Add more issuers via the ``EDINET_CODES`` env-var (comma-separated EDINET codes).

Pipeline (per filing date, driven by pull()):
  1. GET /documents.json?date=YYYY-MM-DD&type=2  → document list for that date
  2. Filter: docTypeCode in {120, 130} (有価証券報告書) AND edinetCode in targets
  3. GET /documents/{docID}?type=5               → ZIP archive of CSV packages
  4. Find the primary jpcrp_corp-*.csv inside the ZIP
  5. Parse tab-separated rows; extract revenue elements with dimensional contexts
  6. Yield one verbatim dict per segment/geography fact

normalize() maps each verbatim dict to typed raw_filings columns::

    filer        → filer_name from the document list (Japanese, e.g. 村田製作所)
    ticker       → 4-digit TSE code derived from secCode (e.g. "6981")
    period       → ISO period string built from periodStart/periodEnd ("FY2024-03")
    segment      → business-segment name (populated for business-segment rows)
    geography    → geographic-segment name (populated for geography rows)
    revenue_usd  → value converted JPY → USD via EDINET_JPY_USD_RATE env var
    doc_url      → canonical EDINET document URL

Revenue is stored in full USD (not millions); the ``filings_segment_extraction``
method divides by 1e6 when writing cell_triangulation.estimate_usd_m.

FX conversion: reads ``EDINET_JPY_USD_RATE`` (float, default 0.0067, ≈ mid-2024
rate). For production, this should be supplied from raw_external_metrics (World
Bank / FRED USD/JPY series).  If the env var is invalid or absent the default is
used and a one-time warning is logged.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import zipfile
from datetime import date, timedelta
from typing import Any, Iterator

from connectors.base import (
    Connector,
    ProbeResult,
    classify_exception,
    classify_http_error,
)
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.edinet")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"

# EDINET codes for Phase-1 Japanese passive-component makers.
# Extend at runtime via EDINET_CODES env var.
_DEFAULT_EDINET_CODES: frozenset[str] = frozenset({"E01150", "E01164", "E01167"})

# Static metadata for known targets (used as fallback when API omits fields).
_EDINET_META: dict[str, dict[str, str]] = {
    "E01150": {"name": "Murata Manufacturing Co., Ltd.", "ticker": "6981"},
    "E01164": {"name": "TDK Corporation",                "ticker": "6762"},
    "E01167": {"name": "Taiyo Yuden Co., Ltd.",          "ticker": "6976"},
}

# Annual securities report (有価証券報告書) and foreign-company variant.
_ANNUAL_DOC_TYPE_CODES: frozenset[str] = frozenset({"120", "130"})

# Revenue element IDs that appear in EDINET CSV packages.
# Covers both IFRS reporters (jpcrp_cor) and JGAAP reporters (jppfs_cor).
_REVENUE_ELEMENTS: frozenset[str] = frozenset({
    # IFRS — explicit "Revenue" or "Net Sales"
    "jpcrp_cor:RevenueIFRS",
    "jpcrp_cor:NetSalesIFRS",
    "jpcrp_cor:RevenueFromContractsWithCustomersIFRS",
    "jpcrp_cor:Revenue",
    # IFRS summary fields used in key financial data sections
    "jpcrp_cor:NetSalesIFRSSummaryOfBusinessResults",
    "jpcrp_cor:RevenueIFRSSummaryOfBusinessResults",
    # JGAAP
    "jppfs_cor:NetSales",
    "jppfs_cor:OperatingRevenue",
    "jppfs_cor:NetSalesAndOperatingRevenues",
    "jppfs_cor:NetSalesSummaryOfBusinessResults",
})

# Lower-cased keyword fragments that flag a context Member as a geographic segment
# rather than a product/business segment.
_GEO_MEMBER_KEYWORDS: frozenset[str] = frozenset({
    "japan", "domestic", "china", "asia", "americas", "america",
    "europe", "emea", "apac", "northamerica", "southamerica", "latin",
    "overseas", "foreign", "international", "india", "korea", "taiwan",
    "oceania", "africa", "middleeast", "other",
})

# CSV column header strings (EDINET uses Japanese column names).
_COL_ELEMENT  = "要素ID"
_COL_CONTEXT  = "コンテキストID"
_COL_YEAR     = "相対年度"
_COL_CONSOL   = "連結・個別"
_COL_LABEL_JA = "ラベル（日本語）"
_COL_LABEL_EN = "ラベル（英語）"
_COL_UNIT     = "単位"
_COL_VALUE    = "値"

# Default fallback FX rate (approximate mid-2024).
_DEFAULT_JPY_USD = 0.0067


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_jpy_usd_rate() -> float:
    """Read JPY→USD rate from env var, falling back to the built-in default."""
    raw = os.environ.get("EDINET_JPY_USD_RATE", "")
    if raw:
        try:
            rate = float(raw)
            if rate > 0:
                return rate
        except ValueError:
            pass
        logger.warning(
            "edinet: EDINET_JPY_USD_RATE=%r is invalid; using default %.6f",
            raw, _DEFAULT_JPY_USD,
        )
    return _DEFAULT_JPY_USD


def _extract_member_name(context_id: str) -> str | None:
    """Return the dimension member name from a context ID, or None for totals.

    EDINET context IDs follow the pattern::

        CurrentYearDuration                          (consolidated total)
        CurrentYearDuration_ComponentsMember         (business segment)
        CurrentYearDuration_JapanMember              (geography segment)
        Prior1YearDuration_AsiaMember                (prior year geography)

    We strip the ``_``-prefix and the ``Member`` suffix to get the label.
    """
    parts = context_id.split("_")
    for part in parts[1:]:
        if part.endswith("Member"):
            return part[:-len("Member")]  # strip trailing "Member"
    return None


def _classify_member(member_name: str) -> str:
    """Return ``'geography'``, ``'business'``, or ``'total'`` for a member name."""
    if not member_name:
        return "total"
    lower = member_name.lower()
    # Check for known geographic keywords.
    if any(kw in lower for kw in _GEO_MEMBER_KEYWORDS):
        return "geography"
    return "business"


def _sniff_delimiter(first_line: str) -> str:
    """Detect whether a CSV line is tab- or comma-separated."""
    tab_count = first_line.count("\t")
    comma_count = first_line.count(",")
    return "\t" if tab_count >= comma_count else ","


def _derive_ticker(sec_code: str | None, edinet_code: str) -> str:
    """Derive the 4-digit TSE ticker from EDINET's 5-digit secCode (drops trailing '0').

    Falls back to the static _EDINET_META table, then to the EDINET code itself.
    """
    if sec_code and len(sec_code) >= 5 and sec_code != "00000":
        return sec_code[:4]
    meta = _EDINET_META.get(edinet_code, {})
    return meta.get("ticker", edinet_code)


def _period_label(period_start: str | None, period_end: str | None) -> str:
    """Build a compact period label like ``FY2024-03`` from ISO date strings."""
    if period_end:
        # e.g. "2024-03-31" → "FY2024-03"
        parts = period_end[:7].split("-")  # ["2024","03"]
        if len(parts) == 2:
            return f"FY{parts[0]}-{parts[1]}"
    if period_start:
        parts = period_start[:4]
        return f"FY{parts}"
    return "FYunknown"


# ---------------------------------------------------------------------------
# Main connector class
# ---------------------------------------------------------------------------

@register("edinet")
class EdinetConnector(Connector):
    """Connector for Japan FSA EDINET v2 — structured segment/geographic revenue.

    The connector authenticates with a free Subscription-Key and polls the
    EDINET document list endpoint daily to discover annual reports (docTypeCode
    ``120`` / ``130``) filed by the configured target issuers.  For each
    discovered filing it downloads the companion CSV archive (type=5), parses
    business-segment and geographic-segment revenue rows, and yields them as
    verbatim dicts for the pipeline's normalize → upsert path.
    """

    source_id: str = "edinet"
    raw_table:  str = "raw_filings"

    # Polite throttle: EDINET v2 has no published rate limit; 0.5 s between
    # calls is conservative and keeps daily poll traffic well under 200 calls.
    min_interval: float = 0.5

    def __init__(self, source_row: dict[str, Any], credential: str | None) -> None:
        super().__init__(source_row, credential)
        # Merge env-var extra codes with the built-in default set.
        self._edinet_codes: set[str] = set(_DEFAULT_EDINET_CODES)
        extra_env = os.environ.get("EDINET_CODES", "")
        if extra_env:
            for code in extra_env.split(","):
                code = code.strip()
                if code:
                    self._edinet_codes.add(code)
        self._jpy_usd: float = _load_jpy_usd_rate()

    # ------------------------------------------------------------------ #
    # Auth helpers
    # ------------------------------------------------------------------ #

    def _params(self, **extra: Any) -> dict[str, Any]:
        """Build query-parameter dict, appending the Subscription-Key when available."""
        params: dict[str, Any] = dict(extra)
        if self.credential:
            params["Subscription-Key"] = self.credential
        return params

    def default_headers(self) -> dict[str, str]:
        """EDINET accepts the key as both a query-param and an Azure-style header."""
        headers: dict[str, str] = {}
        if self.credential:
            headers["Ocp-Apim-Subscription-Key"] = self.credential
        return headers

    # ------------------------------------------------------------------ #
    # Connector contract: probe()
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """Test reachability and authentication by fetching yesterday's document list.

        Returns
        -------
        ProbeResult
            ``OK`` if the endpoint responded with a valid JSON payload.
            ``AUTH_FAILED`` if no credential is configured or the API rejects it.
            ``RATE_LIMITED`` / ``UNREACHABLE`` on transient failures.
            ``SCHEMA_MISMATCH`` if the response is not parseable JSON.
        """
        if self.missing_credential():
            return self.auth_failed_probe(
                "No Subscription-Key configured for EDINET v2. "
                "Register a free key at "
                "https://disclosure.edinet-fsa.go.jp/webapiregist/presend"
            )

        probe_date = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            resp = self.http.get(
                f"{_BASE_URL}/documents.json",
                params=self._params(date=probe_date, type=2),
            )
        except Exception as exc:
            status, detail = classify_exception(exc)
            return ProbeResult(status, f"probe network error: {detail}")

        if resp.status_code != 200:
            return ProbeResult(
                classify_http_error(resp.status_code, resp.text),
                f"documents.json returned HTTP {resp.status_code}: {resp.text[:200]}",
            )

        try:
            data = resp.json()
        except Exception as exc:
            return ProbeResult("SCHEMA_MISMATCH", f"JSON parse error: {exc}")

        meta = data.get("metadata", {})
        api_status = meta.get("status", "?")
        if api_status != "200" and api_status != 200:
            return ProbeResult(
                "SCHEMA_MISMATCH",
                f"EDINET metadata.status={api_status!r}: {meta.get('message', '')}",
            )

        count = meta.get("resultset", {}).get("count", "?")
        sample = data.get("results", [])[:1]
        return ProbeResult(
            "OK",
            f"documents.json OK for {probe_date}: {count} document(s) filed",
            sample,
        )

    # ------------------------------------------------------------------ #
    # Connector contract: pull()
    # ------------------------------------------------------------------ #

    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield one verbatim dict per revenue fact row found in EDINET annual reports.

        Iterates each calendar day from *since* (or 365 days ago) to today,
        fetches the EDINET document list, filters for target issuers and annual
        report types, downloads the CSV archive, and emits typed facts.

        Parameters
        ----------
        taxonomy:
            Active taxonomy spine rows (used for context; not yet filtered here).
        geographies:
            Active geography rows (same).
        since:
            ISO-8601 date (``YYYY-MM-DD``) for lookback start.  ``None`` → 12-month
            default.  The pipeline passes the last successful pull timestamp.
        """
        if self.missing_credential():
            logger.warning(
                "edinet: no Subscription-Key — skipping pull (set cred/edinet)"
            )
            return

        start_date = (
            date.fromisoformat(since[:10])
            if since
            else date.today() - timedelta(days=365)
        )
        end_date = date.today()

        logger.info(
            "edinet: polling document list %s → %s for codes %s",
            start_date, end_date, sorted(self._edinet_codes),
        )

        current = start_date
        while current <= end_date:
            date_str = current.strftime("%Y-%m-%d")
            current += timedelta(days=1)

            docs = self._fetch_document_list(date_str)
            for doc in docs:
                edinet_code  = doc.get("edinetCode", "")
                doc_type     = doc.get("docTypeCode", "")
                doc_id       = doc.get("docID", "")

                if edinet_code not in self._edinet_codes:
                    continue
                if doc_type not in _ANNUAL_DOC_TYPE_CODES:
                    continue
                if not doc_id:
                    continue

                logger.info(
                    "edinet: found annual report %s (%s %s %s)",
                    doc_id, edinet_code, doc.get("filerName", ""), doc_type,
                )

                yield from self._pull_csv_facts(doc_id, doc)

    def _fetch_document_list(self, date_str: str) -> list[dict[str, Any]]:
        """Fetch the EDINET document list for one date; return result list (may be empty)."""
        try:
            resp = self.http.get(
                f"{_BASE_URL}/documents.json",
                params=self._params(date=date_str, type=2),
            )
        except Exception as exc:
            status, detail = classify_exception(exc)
            logger.warning("edinet: documents.json %s %s: %s", date_str, status, detail)
            return []

        if resp.status_code != 200:
            logger.warning(
                "edinet: documents.json %s HTTP %s",
                date_str, resp.status_code,
            )
            return []

        try:
            return resp.json().get("results", [])
        except Exception as exc:
            logger.warning("edinet: JSON parse error for %s: %s", date_str, exc)
            return []

    def _pull_csv_facts(
        self, doc_id: str, meta: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        """Download the CSV package for *doc_id* and yield one dict per revenue fact."""
        doc_url = f"{_BASE_URL}/documents/{doc_id}"

        try:
            resp = self.http.get(doc_url, params=self._params(type=5))
        except Exception as exc:
            status, detail = classify_exception(exc)
            logger.warning("edinet: CSV download %s %s: %s", doc_id, status, detail)
            return

        if resp.status_code != 200:
            logger.warning(
                "edinet: CSV %s HTTP %s — docTypeCode may not have a CSV package",
                doc_id, resp.status_code,
            )
            return

        # Parse the ZIP archive.
        try:
            archive = zipfile.ZipFile(io.BytesIO(resp.content))
        except zipfile.BadZipFile as exc:
            logger.warning("edinet: ZIP parse error %s: %s", doc_id, exc)
            return

        # Prefer jpcrp_corp-* files (the primary XBRL-to-CSV output).
        csv_candidates = sorted(
            [n for n in archive.namelist() if n.lower().endswith(".csv")],
            key=lambda n: (0 if "jpcrp_corp" in n.lower() else 1, n),
        )

        if not csv_candidates:
            logger.warning("edinet: no CSV files in ZIP for %s", doc_id)
            return

        primary_csv = csv_candidates[0]
        logger.debug("edinet: parsing %s/%s", doc_id, primary_csv)

        try:
            raw_bytes = archive.read(primary_csv)
            text = raw_bytes.decode("utf-8-sig")  # strip UTF-8 BOM if present
        except Exception as exc:
            logger.warning("edinet: could not read %s/%s: %s", doc_id, primary_csv, exc)
            return

        yield from self._parse_csv_rows(text, meta, doc_id)

    def _parse_csv_rows(
        self,
        text: str,
        meta: dict[str, Any],
        doc_id: str,
    ) -> Iterator[dict[str, Any]]:
        """Parse EDINET CSV text and yield one verbatim dict per revenue-segment row.

        The CSV is either tab- or comma-separated (sniffed from the first line).
        Column positions are resolved dynamically from the header row so the
        connector tolerates minor EDINET schema additions without breaking.
        """
        lines = text.splitlines()
        if not lines:
            logger.warning("edinet: empty CSV for %s", doc_id)
            return

        delimiter = _sniff_delimiter(lines[0])

        reader = csv.reader(io.StringIO(text), delimiter=delimiter)

        # Read header row to map column names to indices.
        try:
            header = next(reader)
        except StopIteration:
            logger.warning("edinet: CSV has no header row for %s", doc_id)
            return

        def _col(name: str) -> int | None:
            try:
                return header.index(name)
            except ValueError:
                return None

        idx_element  = _col(_COL_ELEMENT)
        idx_context  = _col(_COL_CONTEXT)
        idx_year     = _col(_COL_YEAR)
        idx_consol   = _col(_COL_CONSOL)
        idx_label_ja = _col(_COL_LABEL_JA)
        idx_label_en = _col(_COL_LABEL_EN)
        idx_unit     = _col(_COL_UNIT)
        idx_value    = _col(_COL_VALUE)

        if any(i is None for i in (idx_element, idx_context, idx_unit, idx_value)):
            logger.warning(
                "edinet: CSV header missing required columns for %s; "
                "found: %s", doc_id, header,
            )
            return

        edinet_code   = meta.get("edinetCode", "")
        filer_name    = meta.get("filerName", "") or _EDINET_META.get(edinet_code, {}).get("name", edinet_code)
        sec_code      = meta.get("secCode", "")
        ticker        = _derive_ticker(sec_code, edinet_code)
        period_start  = meta.get("periodStart", "")
        period_end    = meta.get("periodEnd", "")
        period_label  = _period_label(period_start, period_end)
        doc_url       = f"{_BASE_URL}/documents/{doc_id}"

        rows_emitted = 0
        for row in reader:
            if not row or len(row) <= max(
                i for i in (idx_element, idx_context, idx_unit, idx_value)
                if i is not None
            ):
                continue

            element_id = row[idx_element].strip()
            context_id = row[idx_context].strip()
            unit       = row[idx_unit].strip()
            value_raw  = row[idx_value].strip()

            # Keep only revenue-class elements.
            if element_id not in _REVENUE_ELEMENTS:
                continue

            # Skip blank or non-numeric values.
            if not value_raw:
                continue
            try:
                value_num = float(value_raw.replace(",", ""))
            except ValueError:
                continue

            # Identify the dimensional member (segment or geo label).
            member_name = _extract_member_name(context_id)

            # Skip context IDs that represent multi-period comparatives or
            # prior-year comparative totals — we only want current-year.
            ctx_lower = context_id.lower()
            if "prior" in ctx_lower and "member" not in ctx_lower:
                continue

            segment_type = _classify_member(member_name or "")

            # Build the verbatim dict (raw payload stored in raw_json).
            payload: dict[str, Any] = {
                # Document identity
                "_edinet_code":   edinet_code,
                "_doc_id":        doc_id,
                "_source_id":     self.source_id,
                "_doc_url":       doc_url,
                "_filer_name":    filer_name,
                "_ticker":        ticker,
                "_sec_code":      sec_code,
                "_period_start":  period_start,
                "_period_end":    period_end,
                "_period_label":  period_label,
                "_submit_dt":     meta.get("submitDateTime", ""),
                "_doc_type_code": meta.get("docTypeCode", ""),
                # CSV row fields
                "element_id":     element_id,
                "context_id":     context_id,
                "relative_year":  row[idx_year].strip()    if idx_year     is not None else "",
                "consolidation":  row[idx_consol].strip()  if idx_consol   is not None else "",
                "label_ja":       row[idx_label_ja].strip() if idx_label_ja is not None else "",
                "label_en":       row[idx_label_en].strip() if idx_label_en is not None else "",
                "unit":           unit,
                "value_raw":      value_num,
                # Derived
                "member_name":    member_name or "",
                "segment_type":   segment_type,
            }

            rows_emitted += 1
            yield payload

        if rows_emitted == 0:
            logger.info(
                "edinet: no revenue-element rows found in CSV for %s "
                "(document may not contain segment disclosure)", doc_id,
            )

    # ------------------------------------------------------------------ #
    # Connector contract: normalize()
    # ------------------------------------------------------------------ #

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one verbatim pull() dict to typed raw_filings columns.

        Returns a dict whose keys are a subset of raw_filings typed columns:
        ``filer``, ``ticker``, ``period``, ``segment``, ``geography``,
        ``revenue_usd``, ``doc_url``.

        The ``segment`` and ``geography`` columns are populated exclusively:
        a business-segment row sets ``segment`` and leaves ``geography`` empty;
        a geographic row sets ``geography`` and leaves ``segment`` empty.
        The consolidated total row (segment_type == "total") leaves both empty
        so it can still be stored as a cross-check figure.

        Revenue is converted from the EDINET native unit to USD:

        * JPY (full yen) → multiply by ``_jpy_usd`` rate.
        * Some JGAAP reporters file in thousands of JPY; the unit column carries
          ``"JPY/thousands"`` or similar — divide by 1,000 first.
        * If unit is unrecognised the raw numeric value is passed through
          without conversion and a warning is logged.

        Returns ``{}`` if the payload is missing mandatory fields.
        """
        # Mandatory fields — return empty if missing.
        value_raw = raw.get("value_raw")
        if value_raw is None:
            return {}

        filer_name   = raw.get("_filer_name", "")
        ticker       = raw.get("_ticker", "")
        period       = raw.get("_period_label", "")
        member_name  = raw.get("member_name", "")
        segment_type = raw.get("segment_type", "total")
        doc_url      = raw.get("_doc_url", "")
        unit         = raw.get("unit", "JPY").upper()

        # Unit-aware JPY → USD conversion.
        try:
            jpy_value = float(value_raw)
        except (TypeError, ValueError):
            return {}

        if "JPY" in unit:
            if "THOUSAND" in unit or "/1000" in unit or "KYEN" in unit:
                jpy_value *= 1_000          # thousands-of-yen → full yen
            elif "MILLION" in unit:
                jpy_value *= 1_000_000      # millions-of-yen → full yen
            revenue_usd = jpy_value * self._jpy_usd
        elif unit in ("USD", "US$", "DOLLAR"):
            # Occasionally USD-denominated (foreign company reports).
            revenue_usd = jpy_value
        else:
            logger.warning(
                "edinet: unrecognised unit %r for %s/%s — storing raw value",
                unit, raw.get("_doc_id", "?"), raw.get("element_id", "?"),
            )
            revenue_usd = jpy_value

        # Assign segment / geography exclusively.
        segment_col  = ""
        geography_col = ""

        if segment_type == "business":
            segment_col = member_name
        elif segment_type == "geography":
            geography_col = member_name
        # "total" → both empty (consolidated total)

        return {
            "filer":       filer_name,
            "ticker":      ticker,
            "period":      period,
            "segment":     segment_col,
            "geography":   geography_col,
            "revenue_usd": revenue_usd,
            "doc_url":     doc_url,
        }
