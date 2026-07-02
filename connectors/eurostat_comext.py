"""Eurostat Comext — EU international trade in goods (CN8) connector.

Source ID  : eurostat_comext
Raw table  : raw_trade_flows
Auth       : none (fully open, no registration required)
Class      : A — EU member-state trade statistics, authoritative for EU origin/destination

Data access
-----------
Two access paths are supported, chosen automatically based on the taxonomy size:

**Targeted (small taxonomy, < BULK_THRESHOLD HS codes)**
  Eurostat dissemination statistics API — returns JSON-stat 2.0 parsed by *pyjstat*.
  Endpoint::

      https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}

  Dataset ``DS-016894`` is Comext monthly trade data by CN8 (partner × product ×
  flow × EU member state).  One API call per HS-6 code per flow (import/export).

**Bulk download (large taxonomy, ≥ BULK_THRESHOLD HS codes)**
  Monthly gzip-compressed TSV files from the Eurostat BulkDownloadListing.
  The connector downloads, decompresses, and streams rows, filtering in-memory
  to keep only the HS-6 prefixes present in the taxonomy.  Falls back to the
  targeted API path per-month when the bulk file is unavailable.

pyjstat dependency
------------------
*pyjstat* (``pip install pyjstat``) is required to decode the JSON-stat 2.0
payload returned by the statistics API.  When absent, :meth:`probe` returns
``SCHEMA_MISMATCH`` and :meth:`pull` yields nothing; the rest of the pipeline
continues unaffected.

CN8 → HS-6 mapping
-------------------
Eurostat Comext uses 8-digit Combined Nomenclature (CN8) codes.  The first six
digits of a CN8 code correspond to the HS-6 heading/subheading; digits 7–8 are
EU-internal subdivisions.  This connector:

* Queries with the 6-digit HS prefix padded to 8 digits (``850440`` → ``85044000``)
  to hit the primary CN8 heading.  Bulk pulls capture all sub-codes via prefix
  matching.
* Stores the full 8-digit CN8 code in the typed ``hs_code`` column.
* Sets ``hs_version = "CN8"`` to mark the classification lineage.
* Downstream methods normalise to HS-6 by slicing ``hs_code[:6]``.

Currency
--------
Comext values are reported in **euros (EUR)**, not USD.  They are stored in the
``value_usd`` column (the only numeric value column in the schema) because the
schema does not have a ``value_currency`` column.  The injected key ``_currency``
in the raw verbatim payload (and therefore in ``raw_trade_flows.raw_json``) is
set to ``"EUR"`` so downstream triangulation methods can apply an FX rate.

Rate limits
-----------
No documented rate limit for the statistics API.  Defensive spacing is 0.5 seconds
between requests.  The API imposes a query-size cap (~1000 cells per call); large
product/partner cross-products may require pagination (not yet implemented; the
connector logs a warning and continues with what it receives).
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
from datetime import datetime, timezone
from typing import Any, Iterator

from connectors.base import (
    Connector,
    ProbeResult,
    classify_exception,
    classify_http_error,
)
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.eurostat_comext")

# Eurostat dissemination statistics API — returns JSON-stat 2.0
_STATS_BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"

# Comext monthly trade data by CN8 (the main Comext dataset)
_DATASET = "DS-016894"

# BulkDownloadListing root for Comext monthly files
_BULK_BASE = (
    "https://ec.europa.eu/eurostat/estat-navtree-portlet-prod/BulkDownloadListing"
)

# Prefer bulk download when the taxonomy has this many or more HS-6 codes
BULK_THRESHOLD: int = 20

# Comext flow codes
_FLOW_MAP: dict[str, str] = {
    "1": "IMPORT", "IMP": "IMPORT", "IMPORT": "IMPORT",
    "2": "EXPORT", "EXP": "EXPORT", "EXPORT": "EXPORT",
}

# Probe uses a well-known CN8 code (capacitors / power converters, heading 8504.40)
_PROBE_PRODUCT = "85044000"
_PROBE_YEAR = "2023"


# ------------------------------------------------------------------ #
# Optional pyjstat import — degrade gracefully when absent            #
# ------------------------------------------------------------------ #
try:
    import pyjstat  # type: ignore[import-untyped]

    _HAVE_PYJSTAT = True
except ImportError:
    _HAVE_PYJSTAT = False
    logger.warning(
        "pyjstat is not installed — eurostat_comext connector will be non-functional. "
        "Install with: pip install pyjstat"
    )


@register("eurostat_comext")
class EurostatComextConnector(Connector):
    """Eurostat Comext (CN8, monthly) → raw_trade_flows.

    Each Comext observation becomes one verbatim dict yielded by :meth:`pull` and
    stored in ``raw_trade_flows.raw_json``.  :meth:`normalize` maps it to the typed
    columns (reporter, partner, hs_code, hs_version, flow, period, value_usd, qty,
    qty_unit).
    """

    source_id = "eurostat_comext"
    raw_table = "raw_trade_flows"
    min_interval = 0.5  # no documented limit; be polite to the public API

    # ------------------------------------------------------------------ #
    # Probe                                                                #
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """Cheap health check: single JSON-stat query for a known CN8 code.

        Returns ``SCHEMA_MISMATCH`` immediately when *pyjstat* is not installed,
        because we cannot decode the response without it.
        """
        if not _HAVE_PYJSTAT:
            return ProbeResult(
                "SCHEMA_MISMATCH",
                "pyjstat library not installed; cannot decode Eurostat JSON-stat 2.0 — "
                "install with: pip install pyjstat",
            )

        params: dict[str, str] = {
            "product": _PROBE_PRODUCT,
            "FLOW": "1",           # imports
            "PERIOD": _PROBE_YEAR,
            "format": "JSON",
            "lang": "EN",
        }
        try:
            resp = self.http.get(f"{_STATS_BASE}/{_DATASET}", params=params)
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail)

        if resp.status_code != 200:
            status = classify_http_error(resp.status_code, resp.text)
            return ProbeResult(status, f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            rows = _jsonstat_to_rows(resp.text)
        except Exception as exc:
            return ProbeResult(
                "SCHEMA_MISMATCH", f"JSON-stat parse failure: {exc}"
            )

        if not rows:
            return ProbeResult("EMPTY", "probe query returned no observations")

        sample = {k: v for k, v in rows[0].items() if not k.startswith("_")}
        return ProbeResult("OK", f"HTTP 200; {len(rows)} observation(s)", sample)

    # ------------------------------------------------------------------ #
    # Pull                                                                 #
    # ------------------------------------------------------------------ #

    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield one dict per Comext trade-flow observation.

        Routes to bulk CSV download (preferred, full sub-code coverage) when the
        taxonomy contains :data:`BULK_THRESHOLD` or more distinct HS-6 codes, and
        to targeted JSON-stat API calls otherwise.
        """
        if not _HAVE_PYJSTAT:
            logger.error(
                "eurostat_comext: pyjstat not installed — cannot pull; "
                "install with: pip install pyjstat"
            )
            return

        hs_codes = _extract_hs_codes(taxonomy)
        if not hs_codes:
            logger.info("eurostat_comext: no HS codes in taxonomy — nothing to pull")
            return

        start_year = _parse_since_to_year(since)
        logger.info(
            "eurostat_comext: %d HS-6 code(s) from year %d "
            "(bulk_threshold=%d, using %s path)",
            len(hs_codes), start_year, BULK_THRESHOLD,
            "bulk" if len(hs_codes) >= BULK_THRESHOLD else "targeted",
        )

        if len(hs_codes) >= BULK_THRESHOLD:
            yield from self._pull_bulk(hs_codes, start_year)
        else:
            yield from self._pull_targeted(hs_codes, start_year)

    # ------------------------------------------------------------------ #
    # Targeted API pull (small taxonomy)                                  #
    # ------------------------------------------------------------------ #

    def _pull_targeted(
        self, hs_codes: set[str], start_year: int
    ) -> Iterator[dict[str, Any]]:
        """Query the JSON-stat API for each HS-6 code padded to CN8 + both flows.

        Queries annual periods (YYYY) to minimise call count; Comext monthly
        granularity is available via the ``PERIOD=YYYYMM`` param if needed.

        Note on sub-code coverage: padding HS-6 to 8 digits with zeros queries
        the "00" sub-code of the CN8 heading.  Other EU-specific sub-codes
        (e.g. "85044010") are covered by the bulk path.  When sub-code detail
        matters, set BULK_THRESHOLD low enough to trigger the bulk path.
        """
        current_year = datetime.now(timezone.utc).year - 1  # last complete year

        for hs6 in sorted(hs_codes):
            cn8 = _hs6_to_cn8(hs6)
            for year in range(start_year, current_year + 1):
                for flow_code, flow_label in (("1", "IMPORT"), ("2", "EXPORT")):
                    params: dict[str, str] = {
                        "product": cn8,
                        "FLOW": flow_code,
                        "PERIOD": str(year),
                        "format": "JSON",
                        "lang": "EN",
                    }
                    try:
                        resp = self.http.get(
                            f"{_STATS_BASE}/{_DATASET}", params=params
                        )
                    except Exception as exc:  # noqa: BLE001
                        status, detail = classify_exception(exc)
                        logger.warning(
                            "eurostat_comext targeted %s %s %d: %s — %s",
                            flow_label, cn8, year, status, detail,
                        )
                        continue

                    if resp.status_code != 200:
                        logger.warning(
                            "eurostat_comext targeted %s %s %d: HTTP %d",
                            flow_label, cn8, year, resp.status_code,
                        )
                        continue

                    try:
                        rows = _jsonstat_to_rows(resp.text)
                    except Exception as exc:
                        logger.warning(
                            "eurostat_comext JSON-stat parse error %s %s %d: %s",
                            flow_label, cn8, year, exc,
                        )
                        continue

                    for row in rows:
                        row["_flow"] = flow_label
                        row["_hs6_requested"] = hs6
                        row["_currency"] = "EUR"
                        yield row

    # ------------------------------------------------------------------ #
    # Bulk CSV pull (large taxonomy)                                       #
    # ------------------------------------------------------------------ #

    def _pull_bulk(
        self, hs_codes: set[str], start_year: int
    ) -> Iterator[dict[str, Any]]:
        """Download Comext monthly bulk TSV.gz files and stream filtered rows.

        Iterates backwards from the most recent complete month to the start year.
        Filters rows in-memory, keeping only those whose PRODUCT starts with one
        of the 6-digit HS prefixes in *hs_codes*.

        Falls back to :meth:`_pull_targeted_month` for any month whose bulk file
        is unavailable or fails to decompress.
        """
        now = datetime.now(timezone.utc)
        # Start from the last complete month
        year, month = now.year, now.month - 1
        if month == 0:
            year, month = year - 1, 12

        months_to_pull: list[tuple[int, int]] = []
        y, m = year, month
        while y >= start_year:
            months_to_pull.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12

        hs_prefixes: frozenset[str] = frozenset(hs_codes)
        total_yielded = 0

        for yr, mo in months_to_pull:
            period_str = f"{yr}{mo:02d}"
            yielded_this = 0

            try:
                rows_iter = self._fetch_bulk_month(period_str, hs_prefixes)
                for row in rows_iter:
                    total_yielded += 1
                    yielded_this += 1
                    yield row
            except _BulkUnavailable as exc:
                logger.warning(
                    "eurostat_comext bulk %s unavailable (%s) — falling back to targeted",
                    period_str, exc,
                )
                yield from self._pull_targeted_month(hs_prefixes, yr, mo)
                continue

            logger.debug(
                "eurostat_comext bulk %s: %d row(s) yielded", period_str, yielded_this
            )

        logger.info("eurostat_comext bulk pull complete: %d row(s) total", total_yielded)

    def _fetch_bulk_month(
        self, period_str: str, hs_prefixes: frozenset[str]
    ) -> Iterator[dict[str, Any]]:
        """Download and stream one Comext bulk file, filtering by HS-6 prefix.

        Raises :class:`_BulkUnavailable` when the file cannot be fetched or
        decompressed so the caller can fall back to targeted queries.
        """
        # Comext bulk TSV.gz filename pattern (subject to Eurostat naming changes)
        file_path = (
            f"comext%2FCOMEXT_DATA%2FPRODUCTS%2F{period_str}.tsv.gz"
        )
        url = f"{_BULK_BASE}?sort=1&file={file_path}"

        try:
            resp = self.http.get(url)
        except Exception as exc:  # noqa: BLE001
            raise _BulkUnavailable(f"transport error: {exc}") from exc

        if resp.status_code == 404:
            raise _BulkUnavailable(f"HTTP 404 — file not found: {url}")
        if resp.status_code != 200:
            raise _BulkUnavailable(
                f"HTTP {resp.status_code}: {resp.text[:120]}"
            )

        try:
            raw_bytes = gzip.decompress(resp.content)
        except Exception as exc:
            raise _BulkUnavailable(f"gzip decompress failed: {exc}") from exc

        text = raw_bytes.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")

        for row in reader:
            # PRODUCT column may be named PRODUCT, product, or CN8
            product = (
                row.get("PRODUCT")
                or row.get("product")
                or row.get("CN8")
                or ""
            ).strip().replace(" ", "")

            # Keep only rows matching one of our HS-6 prefixes
            if not any(product.startswith(pfx) for pfx in hs_prefixes):
                continue

            flow_raw = (
                row.get("FLOW") or row.get("flow") or ""
            ).strip().upper()
            row["_flow"] = _FLOW_MAP.get(flow_raw, flow_raw or None)
            row["_period"] = period_str
            row["_currency"] = "EUR"
            yield dict(row)

    def _pull_targeted_month(
        self, hs_prefixes: set[str], year: int, month: int
    ) -> Iterator[dict[str, Any]]:
        """Targeted fallback for a single month when the bulk file is unavailable."""
        period = f"{year}{month:02d}"
        for hs6 in sorted(hs_prefixes):
            cn8 = _hs6_to_cn8(hs6)
            for flow_code, flow_label in (("1", "IMPORT"), ("2", "EXPORT")):
                params: dict[str, str] = {
                    "product": cn8,
                    "FLOW": flow_code,
                    "PERIOD": period,
                    "format": "JSON",
                    "lang": "EN",
                }
                try:
                    resp = self.http.get(f"{_STATS_BASE}/{_DATASET}", params=params)
                    if resp.status_code != 200:
                        continue
                    rows = _jsonstat_to_rows(resp.text)
                    for row in rows:
                        row["_flow"] = flow_label
                        row["_hs6_requested"] = hs6
                        row["_period"] = period
                        row["_currency"] = "EUR"
                        yield row
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "eurostat_comext fallback targeted %s %s %s: %s",
                        flow_label, cn8, period, exc,
                    )

    # ------------------------------------------------------------------ #
    # Normalize                                                            #
    # ------------------------------------------------------------------ #

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one Comext observation dict to raw_trade_flows typed columns.

        The verbatim row dict (with injected ``_flow``, ``_currency``, etc. keys)
        is stored in ``raw_trade_flows.raw_json`` by the pipeline; this method
        derives only the typed columns.

        **Currency note:** ``value_usd`` stores the EUR value as reported by
        Eurostat.  The ``raw_json`` field carries ``_currency: "EUR"`` so
        downstream triangulation methods can apply an EUR→USD exchange rate.
        """
        # Resolve flow direction from the injected key or raw FLOW code
        flow_raw = (
            raw.get("_flow")
            or raw.get("FLOW")
            or raw.get("flow")
            or ""
        ).strip().upper()
        flow = _FLOW_MAP.get(flow_raw, flow_raw or None)

        # CN8 product code — may be in PRODUCT, product, or CN8 column
        cn8 = (
            raw.get("PRODUCT")
            or raw.get("product")
            or raw.get("CN8")
            or ""
        ).strip().replace(" ", "")

        # Normalize CN8 (8-digit) → HS-6 (first 6 digits) for typed column
        hs6 = cn8[:6] if len(cn8) >= 6 else (cn8 or None)

        # Declaring EU member state (the country compiling the stat)
        reporter = (
            raw.get("DECLARANT")
            or raw.get("declarant")
            or raw.get("DECLARANT_ISO")
            or raw.get("REPORTER")
        )

        # Trading partner country
        partner = (
            raw.get("PARTNER")
            or raw.get("partner")
            or raw.get("PARTNER_ISO")
            or raw.get("PARTNER_CPA_A64")
        )

        # Value in euros — stored verbatim; currency noted in raw_json
        value_eur = _to_numeric(
            raw.get("VALUE_IN_EUROS")
            or raw.get("STAT_VALUE")
            or raw.get("value")
            or raw.get("OBS_VALUE")
        )

        # Quantity in 100 kg (Comext standard unit); convert to metric tons
        qty_100kg = _to_numeric(
            raw.get("QUANTITY_IN_100KG")
            or raw.get("QUANTITY")
        )
        qty_mt = qty_100kg / 10.0 if qty_100kg is not None else None

        # Period: may be YYYYMM (monthly), YYYY (annual), or TIME_PERIOD
        period = (
            raw.get("TIME_PERIOD")
            or raw.get("PERIOD")
            or raw.get("period")
            or raw.get("_period")
        )

        return {
            "reporter": reporter,
            "partner": partner,
            "hs_code": hs6,
            "hs_version": "CN8",     # EU Combined Nomenclature 8-digit source
            "flow": flow,
            "period": str(period) if period is not None else None,
            "value_usd": value_eur,  # EUR value; _currency=EUR in raw_json
            "qty": qty_mt,
            "qty_unit": "MT" if qty_mt is not None else None,  # metric tons
        }


# ------------------------------------------------------------------ #
# Internal sentinel exception                                         #
# ------------------------------------------------------------------ #

class _BulkUnavailable(Exception):
    """Raised by _fetch_bulk_month when the bulk file cannot be obtained."""


# ------------------------------------------------------------------ #
# Module-level helpers                                                #
# ------------------------------------------------------------------ #

def _jsonstat_to_rows(text: str) -> list[dict[str, Any]]:
    """Parse a Eurostat JSON-stat 2.0 payload into a list of flat row dicts.

    Uses *pyjstat* to convert the JSON-stat dataset to a pandas DataFrame, then
    serialises each row to a plain dict.  Drops observations where the value is
    null/NaN (gap cells present in the JSON-stat value array).

    Raises :class:`RuntimeError` when pyjstat is unavailable, and re-raises any
    pyjstat/pandas parse error so the caller can classify and log it.
    """
    if not _HAVE_PYJSTAT:
        raise RuntimeError("pyjstat not installed")

    dataset = pyjstat.Dataset.read(text)
    result = dataset.write("dataframe")

    # pyjstat ≥0.3 may return a list of DataFrames when there are multiple
    # metrics; earlier versions return a single DataFrame.
    dfs = result if isinstance(result, list) else [result]

    rows: list[dict[str, Any]] = []
    for df in dfs:
        # Drop null/NaN observation values (sparse JSON-stat gap cells)
        df_clean = df.dropna(subset=["value"])
        rows.extend(df_clean.to_dict(orient="records"))
    return rows


def _extract_hs_codes(taxonomy: list[dict[str, Any]]) -> set[str]:
    """Collect unique HS-6 prefixes from taxonomy_subcategories rows.

    The ``hs_codes`` Postgres TEXT[] column is delivered by the pipeline as a
    Python list.  Codes are normalised: dots and spaces stripped, capped at 6
    digits.
    """
    codes: set[str] = set()
    for row in taxonomy:
        for code in row.get("hs_codes") or []:
            cleaned = str(code).strip().replace(".", "").replace(" ", "")
            if cleaned:
                codes.add(cleaned[:6])
    return codes


def _hs6_to_cn8(hs6: str) -> str:
    """Right-pad a 6-digit HS code to 8 digits (CN8 style) with zeros.

    Example: ``"850440"`` → ``"85044000"``.

    This targets the "00" CN8 sub-code of the HS heading.  EU-specific
    sub-codes ("85044010", "85044020", …) are captured by the bulk path
    which filters by the 6-digit prefix.
    """
    return hs6.ljust(8, "0")[:8]


def _parse_since_to_year(since: str | None) -> int:
    """Convert an ISO-date lookback hint to an integer year.

    Falls back to the prior complete year when *since* is absent or unparseable.
    """
    if since:
        try:
            return int(since[:4])
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc).year - 1


def _to_numeric(value: Any) -> float | None:
    """Coerce a value to float; returns ``None`` for blanks, nulls, and NaN.

    Handles pandas float NaN (from pyjstat DataFrame conversion) as well as
    common Eurostat missing-value markers (``":"`` and ``"N"``).
    """
    if value is None:
        return None
    # Detect pandas NaN without importing numpy
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:  # noqa: BLE001
        pass
    s = str(value).strip()
    if s in ("", "null", "N", "-", ":", "NaN", "nan"):
        return None
    try:
        return float(s.replace(",", ""))
    except (ValueError, TypeError):
        return None
