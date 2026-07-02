"""WHO Global Health Observatory (GHO) OData connector — raw_external_metrics.

Source:  https://ghoapi.azureedge.net/api/   (base URL is configurable)
Auth:    none (open OData service)
Table:   raw_external_metrics
Class:   C (macro scaling / triangulation support)

The GHO exposes an OData v4 REST API.  Key endpoints:

    GET /Indicator                         — catalogue of all indicator codes
    GET /{IndicatorCode}                   — all observations for an indicator
    GET /{IndicatorCode}?$filter=...       — filtered subset
    GET /{IndicatorCode}?$count=true       — include total count in response

Query parameters used by this connector:

    $filter   SpatialDim eq '{ISO3}' and TimeDimType eq 'YEAR'
              and TimeDim ge {start_year}
    $select   Id,IndicatorCode,SpatialDimType,SpatialDim,
              TimeDimType,TimeDim,NumericValue,Low,High,Value,Comments
    $orderby  TimeDim desc
    $top      1 000 (page size)
    $skip     N     (offset for pagination)
    $count    true  (for total-count metadata on first page)

Response envelope:

    {
      "@odata.context": "...",
      "@odata.count": 42,      # present when $count=true
      "value": [ {...}, ... ]  # the data records
    }

Each verbatim record from ``value`` is yielded by pull().  normalize() maps
it onto raw_external_metrics typed columns.

Configuration (optional; stored in source_row["config"] as a JSON object or
source_row["notes"] as a JSON string):

    indicators : list[str]
        WHO GHO indicator codes to pull per geography.
        Default: the 6 series in _DEFAULT_INDICATORS.
    years_back : int
        Lookback window when the ``since`` pull argument is absent.
        Default: 10 years.
    base_url : str
        Override the GHO API base URL (useful for staging or caching proxies).
        Lowest precedence: source_row["url_pattern"] wins if set.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Iterator
from urllib.parse import quote

import httpx

from connectors.base import (
    Connector,
    ProbeResult,
    classify_exception,
    classify_http_error,
)
from connectors.registry import register
from connectors.worldbank import _parse_json_config  # shared utility

logger = logging.getLogger("grx10.connectors.who_gho")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WHO_GHO_BASE = "https://ghoapi.azureedge.net/api"

# Default WHO GHO indicators pulled per active geography.
# These are health/demographic series used as macro scaling factors in
# activity_volume_unit_price and as dimension signals in market-sizing cells.
_DEFAULT_INDICATORS: list[str] = [
    "WHOSIS_000001",   # Life expectancy at birth — both sexes (years)
    "WHOSIS_000015",   # Healthy life expectancy (HALE) at birth — both sexes
    "WHS7_104",        # Total health expenditure as % of GDP
    "WHS7_156",        # Per-capita health expenditure (PPP int. $)
    "MDG_0000000001",  # Under-five mortality rate (per 1 000 live births)
    "PHE_HHAIR_PROP_POP",  # Population using clean fuels for cooking (%)
]

# Country name (from geographies.yaml) -> ISO-3 code used by WHO GHO SpatialDim.
_COUNTRY_ISO3: dict[str, str] = {
    "Afghanistan": "AFG",
    "Argentina": "ARG",
    "Australia": "AUS",
    "Austria": "AUT",
    "Bangladesh": "BGD",
    "Belgium": "BEL",
    "Brazil": "BRA",
    "Canada": "CAN",
    "Chile": "CHL",
    "China": "CHN",
    "Colombia": "COL",
    "Czech Republic": "CZE",
    "Denmark": "DNK",
    "Egypt": "EGY",
    "Finland": "FIN",
    "France": "FRA",
    "Germany": "DEU",
    "Greece": "GRC",
    "Hong Kong": "HKG",
    "Hungary": "HUN",
    "India": "IND",
    "Indonesia": "IDN",
    "Israel": "ISR",
    "Italy": "ITA",
    "Japan": "JPN",
    "Korea": "KOR",
    "South Korea": "KOR",
    "Malaysia": "MYS",
    "Mexico": "MEX",
    "Netherlands": "NLD",
    "New Zealand": "NZL",
    "Nigeria": "NGA",
    "Norway": "NOR",
    "Pakistan": "PAK",
    "Philippines": "PHL",
    "Poland": "POL",
    "Portugal": "PRT",
    "Romania": "ROU",
    "Russia": "RUS",
    "Saudi Arabia": "SAU",
    "Singapore": "SGP",
    "South Africa": "ZAF",
    "Spain": "ESP",
    "Sweden": "SWE",
    "Switzerland": "CHE",
    "Taiwan": "TWN",
    "Thailand": "THA",
    "Turkey": "TUR",
    "UAE": "ARE",
    "Ukraine": "UKR",
    "United Arab Emirates": "ARE",
    "United Kingdom": "GBR",
    "UK": "GBR",
    "United States": "USA",
    "USA": "USA",
    "Vietnam": "VNM",
}

# OData $select fields — keeps payloads lean and avoids undocumented fields.
_SELECT_FIELDS = ",".join([
    "Id",
    "IndicatorCode",
    "SpatialDimType",
    "SpatialDim",
    "TimeDimType",
    "TimeDim",
    "NumericValue",
    "Low",
    "High",
    "Value",
    "Comments",
])

_PAGE_SIZE = 1000


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

@register("who_gho")
class WHOGHOConnector(Connector):
    """Pulls WHO GHO health/demographic time-series into ``raw_external_metrics``.

    For each active geography × configured indicator the connector issues
    paginated OData requests (``$skip`` / ``$top``) and yields verbatim OData
    entity dicts.  It filters to ``TimeDimType eq 'YEAR'`` so only annual
    observations land in the raw table (monthly/quarterly GHO data is excluded
    at this level).

    Auth: none — the GHO OData service is fully open.
    Rate-limiting: no published limit; we throttle at 1 req/s by default.
    """

    source_id: str = "who_gho_odata"
    raw_table: str = "raw_external_metrics"
    min_interval: float = 1.0  # polite: 1 req/s — no published GHO rate limit

    _DEFAULT_YEARS_BACK: int = 10

    def __init__(self, source_row: dict[str, Any], credential: str | None) -> None:
        super().__init__(source_row, credential)
        cfg = _parse_json_config(source_row)
        self.indicators: list[str] = list(cfg.get("indicators") or _DEFAULT_INDICATORS)
        self.years_back: int = int(cfg.get("years_back") or self._DEFAULT_YEARS_BACK)
        # Priority: source_row.url_pattern > cfg["base_url"] > module default.
        self._base: str = (
            self.base_url
            or cfg.get("base_url")
            or _WHO_GHO_BASE
        ).rstrip("/")

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _country_iso3(self, country_name: str) -> str | None:
        """Map a geography country name to a WHO GHO ISO-3 code, or None."""
        return _COUNTRY_ISO3.get(country_name)

    def _start_year(self, since: str | None) -> int:
        """Derive the earliest year to request from the ``since`` hint."""
        current_year = date.today().year
        if since:
            try:
                return int(since[:4])
            except (ValueError, TypeError):
                pass
        return current_year - self.years_back

    def _odata_filter(self, iso3: str, start_year: int) -> str:
        """Build the OData ``$filter`` expression for a country × year window."""
        # OData string literals must be single-quoted; numeric literals are bare.
        return (
            f"SpatialDim eq '{iso3}' "
            f"and TimeDimType eq 'YEAR' "
            f"and TimeDim ge {start_year}"
        )

    def _get_data_page(
        self,
        indicator: str,
        iso3: str,
        start_year: int,
        skip: int,
    ) -> httpx.Response:
        """Issue one paginated OData GET for a single indicator × country."""
        url = f"{self._base}/{indicator}"
        params: dict[str, Any] = {
            "$filter": self._odata_filter(iso3, start_year),
            "$select": _SELECT_FIELDS,
            "$orderby": "TimeDim desc",
            "$top": _PAGE_SIZE,
            "$skip": skip,
        }
        if skip == 0:
            # Request count only on the first page to avoid repeating the scan.
            params["$count"] = "true"
        return self.http.get(url, params=params)

    def _probe_indicator_catalogue(self) -> httpx.Response:
        """GET a tiny slice of /Indicator to verify API connectivity."""
        url = f"{self._base}/Indicator"
        return self.http.get(url, params={"$top": 5, "$select": "IndicatorCode,IndicatorName"})

    @staticmethod
    def _parse_odata_response(resp: httpx.Response) -> tuple[int | None, list[dict[str, Any]]] | None:
        """Parse an OData response envelope.

        Returns ``(odata_count, records)`` where ``odata_count`` is ``None``
        when the ``@odata.count`` key was not requested (i.e. skip > 0 pages).
        Returns ``None`` on parse failure.
        """
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(payload, dict) or "value" not in payload:
            return None
        records = payload.get("value") or []
        if not isinstance(records, list):
            return None
        count: int | None = payload.get("@odata.count")
        if count is not None:
            try:
                count = int(count)
            except (TypeError, ValueError):
                count = None
        return count, records

    # ------------------------------------------------------------------ #
    # Connector contract
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """Check GHO API reachability via the /Indicator catalogue endpoint.

        Requests only 5 indicator-code rows — cheap and avoids pulling any
        time-series data during the health check.
        """
        try:
            resp = self._probe_indicator_catalogue()
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail, None)

        if resp.status_code >= 300:
            status = classify_http_error(resp.status_code, resp.text)
            return ProbeResult(
                status, f"HTTP {resp.status_code}: {resp.text[:200]}", None
            )

        parsed = self._parse_odata_response(resp)
        if parsed is None:
            return ProbeResult(
                "SCHEMA_MISMATCH",
                f"Response is not a valid OData envelope: {resp.text[:200]}",
                None,
            )
        _, records = parsed
        if not records:
            return ProbeResult(
                "EMPTY",
                "Reachable (HTTP 200) but /Indicator returned no records",
                None,
            )
        return ProbeResult(
            "OK",
            f"HTTP 200; /Indicator catalogue returned {len(records)} record(s) on probe",
            records[0],
        )

    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield verbatim WHO GHO OData entity dicts for all geographies × indicators.

        Each yielded dict is one annual observation from the GHO ``value`` array.
        ``_source_country`` is injected as a convenience tag so downstream
        normalization has the full country name alongside the ISO-3 spatial dim.

        Pagination is handled via ``$skip`` / ``$top``.  When the server returns
        an ``@odata.count``, we use it to detect the last page; otherwise we stop
        when a page returns fewer records than the page size.
        """
        start_year = self._start_year(since)

        # Deduplicate countries and map to ISO-3 codes.
        seen: set[str] = set()
        countries: list[tuple[str, str]] = []
        for geo in geographies:
            name = (geo.get("country") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            iso3 = self._country_iso3(name)
            if iso3 is None:
                logger.warning(
                    "%s: no ISO-3 mapping for geography country %r — skipping",
                    self.source_id,
                    name,
                )
            else:
                countries.append((name, iso3))

        if not countries:
            logger.info(
                "%s: no mappable countries in geographies list (%d entries); "
                "yielding nothing",
                self.source_id,
                len(geographies),
            )
            return

        for country_name, iso3 in countries:
            for indicator in self.indicators:
                logger.debug(
                    "%s: pulling %s / %s (from %d)",
                    self.source_id,
                    iso3,
                    indicator,
                    start_year,
                )
                skip = 0
                total_count: int | None = None
                while True:
                    try:
                        resp = self._get_data_page(indicator, iso3, start_year, skip)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "%s: request failed for %s/%s skip=%d: %s",
                            self.source_id,
                            iso3,
                            indicator,
                            skip,
                            exc,
                        )
                        break  # stop this pair; continue with the next

                    if resp.status_code >= 300:
                        logger.info(
                            "%s: HTTP %s for %s/%s — stopping this indicator",
                            self.source_id,
                            resp.status_code,
                            iso3,
                            indicator,
                        )
                        break

                    parsed = self._parse_odata_response(resp)
                    if parsed is None:
                        logger.warning(
                            "%s: non-OData response for %s/%s skip=%d: %s",
                            self.source_id,
                            iso3,
                            indicator,
                            skip,
                            resp.text[:200],
                        )
                        break

                    count_this_page, records = parsed

                    # Capture total count from the first page response.
                    if total_count is None and count_this_page is not None:
                        total_count = count_this_page

                    if not records:
                        break

                    for record in records:
                        if isinstance(record, dict):
                            record.setdefault("_source_country", country_name)
                            yield record

                    skip += len(records)

                    # Pagination stop conditions:
                    # (a) fewer records than page size → short/last page
                    if len(records) < _PAGE_SIZE:
                        break
                    # (b) we've consumed all records according to the server count
                    if total_count is not None and skip >= total_count:
                        break

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one verbatim GHO OData entity to ``raw_external_metrics`` columns.

        Column mapping:

        ============  ============================================================
        Column        Source field
        ============  ============================================================
        indicator     ``IndicatorCode`` — the GHO code (e.g. ``WHOSIS_000001``)
        country       ``SpatialDim``    — ISO-3 country code (e.g. ``CHN``)
        period        ``TimeDim``       — year integer, cast to string (e.g. ``"2022"``)
        value         ``NumericValue``  — primary numeric value (``Value`` is a
                                         formatted string that may include a CI
                                         range such as ``"72.5 [70.2-74.8]"``)
        unit          ``""``            — GHO does not embed units per data-point;
                                         unit metadata lives at the indicator level
        ============  ============================================================

        Returns ``{}`` only when both ``IndicatorCode`` and ``SpatialDim`` are
        absent (structurally invalid record).
        """
        indicator_code: str | None = raw.get("IndicatorCode") or None
        country: str | None = raw.get("SpatialDim") or None

        if not indicator_code and not country:
            # Structurally invalid — cannot be attributed; return empty.
            return {}

        time_dim = raw.get("TimeDim")
        period: str | None = str(time_dim) if time_dim is not None else None

        raw_value = raw.get("NumericValue")
        value: float | None = None
        if raw_value is not None:
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                logger.debug(
                    "%s: cannot cast NumericValue %r to float for %s/%s",
                    self.source_id,
                    raw_value,
                    indicator_code,
                    country,
                )

        return {
            "indicator": indicator_code,
            "country": country,
            "period": period,
            "value": value,
            "unit": "",  # unit lives at indicator-catalogue level, not per record
        }
