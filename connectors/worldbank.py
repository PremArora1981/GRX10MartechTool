"""World Bank Indicators connector — raw_external_metrics.

Source:  https://api.worldbank.org/v2
Auth:    none (no credential required)
Table:   raw_external_metrics
Class:   C (macro scaling / triangulation support)

Response envelope (JSON API v2):
    GET /v2/country/{iso2}/indicator/{code}?format=json&per_page=1000&page=N
    -> [ {page, pages, per_page, total, ...}, [ {record}, ... ] ]

The response is always a 2-element JSON array:
  [0]  metadata dict (pagination info)
  [1]  list of data-point dicts (may be null when no data)

Each data-point dict yielded verbatim from pull() is a single time-period
observation for one country × indicator combination.  normalize() maps it
onto raw_external_metrics typed columns.

Configuration (optional; stored in source_row["config"] as JSON or
source_row["notes"] as a JSON string):
    indicators : list[str]
        World Bank indicator codes to pull per geography.
        Default: the 13 macro-economic series in _DEFAULT_INDICATORS.
    years_back : int
        Lookback window used when the `since` pull argument is absent.
        Default: 10 years.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Iterator

import httpx

from connectors.base import (
    Connector,
    ProbeResult,
    classify_exception,
    classify_http_error,
)
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.worldbank")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WB_BASE = "https://api.worldbank.org/v2"

# Default set of World Bank indicator codes pulled for every active geography.
# Covers macro-economic inputs used by the activity_volume_unit_price and
# customs_reconciliation estimation methods.
_DEFAULT_INDICATORS: list[str] = [
    "NY.GDP.MKTP.CD",     # GDP, current USD
    "NY.GDP.PCAP.CD",     # GDP per capita, current USD
    "SP.POP.TOTL",        # Total population
    "NV.IND.MANF.CD",    # Manufacturing value added, current USD
    "NV.IND.TOTL.CD",    # Industry (incl. construction) value added, current USD
    "NE.EXP.GNFS.CD",    # Exports of goods and services, current USD
    "NE.IMP.GNFS.CD",    # Imports of goods and services, current USD
    "TX.VAL.MRCH.CD.WT",  # Merchandise exports (BOP), current USD
    "TM.VAL.MRCH.CD.WT",  # Merchandise imports (BOP), current USD
    "EG.USE.ELEC.KH.PC",  # Electric power consumption, kWh per capita
    "EG.ELC.ACCS.ZS",    # Access to electricity, % of population
    "NE.GDI.TOTL.CD",    # Gross capital formation, current USD
    "BX.KLT.DINV.CD.WD", # Foreign direct investment, net inflows, current USD
]

# Mapping from country names used in geographies.yaml to World Bank ISO-2 codes.
# The WB v2 API accepts ISO-2 country codes in the path:
#   /v2/country/{iso2}/indicator/{code}
_COUNTRY_ISO2: dict[str, str] = {
    "Afghanistan": "AF",
    "Argentina": "AR",
    "Australia": "AU",
    "Austria": "AT",
    "Bangladesh": "BD",
    "Belgium": "BE",
    "Brazil": "BR",
    "Canada": "CA",
    "Chile": "CL",
    "China": "CN",
    "Colombia": "CO",
    "Czech Republic": "CZ",
    "Denmark": "DK",
    "Egypt": "EG",
    "Finland": "FI",
    "France": "FR",
    "Germany": "DE",
    "Greece": "GR",
    "Hong Kong": "HK",
    "Hungary": "HU",
    "India": "IN",
    "Indonesia": "ID",
    "Israel": "IL",
    "Italy": "IT",
    "Japan": "JP",
    "Korea": "KR",
    "South Korea": "KR",
    "Malaysia": "MY",
    "Mexico": "MX",
    "Netherlands": "NL",
    "New Zealand": "NZ",
    "Nigeria": "NG",
    "Norway": "NO",
    "Pakistan": "PK",
    "Philippines": "PH",
    "Poland": "PL",
    "Portugal": "PT",
    "Romania": "RO",
    "Russia": "RU",
    "Saudi Arabia": "SA",
    "Singapore": "SG",
    "South Africa": "ZA",
    "Spain": "ES",
    "Sweden": "SE",
    "Switzerland": "CH",
    "Taiwan": "TW",
    "Thailand": "TH",
    "Turkey": "TR",
    "UAE": "AE",
    "Ukraine": "UA",
    "United Arab Emirates": "AE",
    "United Kingdom": "GB",
    "UK": "GB",
    "United States": "US",
    "USA": "US",
    "Vietnam": "VN",
}


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

@register("worldbank")
class WorldBankConnector(Connector):
    """Pulls World Bank indicator time-series into ``raw_external_metrics``.

    The connector iterates every active geography × configured indicator and
    pages through the World Bank JSON v2 API, yielding one verbatim data-point
    dict per pull() yield.  It never fabricates rows: when a country/indicator
    pair returns no data the pair is silently skipped.

    Auth: none required; the WB API is open.  ``requires_auth()`` will be False
    because sources.yaml seeds ``auth: none``.

    Rate-limiting: the WB API has no published rate limit.  We throttle at 1
    request per second (``min_interval = 1.0``) as a polite default; increase
    via the source row's connection config if batching is needed.
    """

    source_id: str = "world_bank"
    raw_table: str = "raw_external_metrics"
    min_interval: float = 1.0  # polite: 1 req/s — no published WB rate limit

    _DEFAULT_YEARS_BACK: int = 10

    def __init__(self, source_row: dict[str, Any], credential: str | None) -> None:
        super().__init__(source_row, credential)
        cfg = _parse_json_config(source_row)
        self.indicators: list[str] = list(cfg.get("indicators") or _DEFAULT_INDICATORS)
        self.years_back: int = int(cfg.get("years_back") or self._DEFAULT_YEARS_BACK)
        # Allow override via url_pattern (e.g. a caching proxy or test fixture).
        self._base: str = (self.base_url or _WB_BASE).rstrip("/")

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _country_iso2(self, country_name: str) -> str | None:
        """Map a geography country name to a World Bank ISO-2 code, or None."""
        return _COUNTRY_ISO2.get(country_name)

    def _year_range(self, since: str | None) -> str:
        """Derive a WB date-range string (``YYYY:YYYY``) from the ``since`` hint.

        ``since`` is an ISO-date string (e.g. ``"2020-01-01"``).  If absent,
        the lookback defaults to ``years_back`` years before today.
        """
        current_year = date.today().year
        if since:
            try:
                start_year = int(since[:4])
            except (ValueError, TypeError):
                start_year = current_year - self.years_back
        else:
            start_year = current_year - self.years_back
        return f"{start_year}:{current_year}"

    def _get_page(
        self, iso2: str, indicator: str, date_range: str, page: int
    ) -> httpx.Response:
        """Issue one paginated GET to the WB indicator endpoint."""
        url = f"{self._base}/country/{iso2}/indicator/{indicator}"
        return self.http.get(
            url,
            params={
                "format": "json",
                "per_page": 1000,
                "date": date_range,
                "page": page,
            },
        )

    @staticmethod
    def _parse_envelope(resp: httpx.Response) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Parse the WB 2-element JSON array response.

        Returns ``(meta_dict, records_list)`` or ``None`` when the response
        body does not conform to the expected envelope structure.

        The WB API returns ``null`` for the data element when there are no
        observations; this method normalises that to an empty list.
        """
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(payload, list) or len(payload) < 2:
            return None
        meta = payload[0] if isinstance(payload[0], dict) else {}
        data = payload[1]
        records: list[dict[str, Any]] = data if isinstance(data, list) else []
        return meta, records

    # ------------------------------------------------------------------ #
    # Connector contract
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """Check WB API reachability with a minimal single-indicator request.

        Uses the US GDP indicator for 2022 — a stable data point guaranteed
        to exist — to verify the API endpoint, JSON envelope, and pagination
        metadata without pulling large volumes.
        """
        probe_indicator = self.indicators[0] if self.indicators else "NY.GDP.MKTP.CD"
        try:
            resp = self._get_page("US", probe_indicator, "2022:2022", page=1)
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail, None)

        if resp.status_code >= 300:
            status = classify_http_error(resp.status_code, resp.text)
            return ProbeResult(
                status, f"HTTP {resp.status_code}: {resp.text[:200]}", None
            )

        parsed = self._parse_envelope(resp)
        if parsed is None:
            return ProbeResult(
                "SCHEMA_MISMATCH",
                f"Response does not match WB 2-element array envelope: "
                f"{resp.text[:200]}",
                None,
            )
        meta, records = parsed
        if not records:
            return ProbeResult(
                "EMPTY",
                f"Reachable (HTTP 200) but no data records in envelope "
                f"(meta={meta})",
                meta,
            )
        return ProbeResult(
            "OK",
            f"HTTP 200; total={meta.get('total', '?')} records across "
            f"{meta.get('pages', '?')} page(s)",
            records[0],
        )

    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield verbatim WB data-point dicts for all active geographies × indicators.

        Each yielded dict is a single observation from the WB API (one country,
        one indicator, one year).  ``_source_country`` is added as an internal
        tag so ``normalize()`` has the full country name alongside the ISO codes
        returned by the API.

        Stops iteration for a given country/indicator pair on HTTP errors or
        malformed responses — it never fabricates rows.
        """
        date_range = self._year_range(since)

        # Build a deduplicated list of (country_name, iso2) from the active geographies.
        seen: set[str] = set()
        countries: list[tuple[str, str]] = []
        for geo in geographies:
            name = (geo.get("country") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            iso2 = self._country_iso2(name)
            if iso2 is None:
                logger.warning(
                    "%s: no ISO-2 mapping for geography country %r — skipping",
                    self.source_id,
                    name,
                )
            else:
                countries.append((name, iso2))

        if not countries:
            logger.info(
                "%s: no mappable countries in geographies list (%d entries); "
                "yielding nothing",
                self.source_id,
                len(geographies),
            )
            return

        for country_name, iso2 in countries:
            for indicator in self.indicators:
                logger.debug(
                    "%s: pulling %s / %s / %s",
                    self.source_id,
                    iso2,
                    indicator,
                    date_range,
                )
                page = 1
                while True:
                    try:
                        resp = self._get_page(iso2, indicator, date_range, page)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "%s: request failed for %s/%s page %d: %s",
                            self.source_id,
                            iso2,
                            indicator,
                            page,
                            exc,
                        )
                        break  # stop this indicator; continue with the next

                    if resp.status_code >= 300:
                        logger.info(
                            "%s: HTTP %s for %s/%s — stopping this indicator",
                            self.source_id,
                            resp.status_code,
                            iso2,
                            indicator,
                        )
                        break

                    parsed = self._parse_envelope(resp)
                    if parsed is None:
                        logger.warning(
                            "%s: unexpected response envelope for %s/%s page %d: %s",
                            self.source_id,
                            iso2,
                            indicator,
                            page,
                            resp.text[:200],
                        )
                        break

                    meta, records = parsed
                    if not records:
                        break  # no (more) data — stop pagination for this pair

                    for record in records:
                        if isinstance(record, dict):
                            # Tag with the canonical name so downstream has it.
                            record.setdefault("_source_country", country_name)
                            yield record

                    total_pages = int(meta.get("pages") or 1)
                    if page >= total_pages:
                        break
                    page += 1

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one verbatim WB data-point to ``raw_external_metrics`` columns.

        The WB record has nested ``indicator`` and ``country`` sub-objects.
        We prefer ``countryiso3code`` for the country column (canonical ISO-3)
        and fall back to ``country.id`` (ISO-2).

        ``value`` will be ``None`` when the WB observation is missing (the API
        uses JSON ``null`` for unreported values, which is faithfully preserved).
        """
        indicator_block = raw.get("indicator") or {}
        country_block = raw.get("country") or {}

        indicator_code: str | None = indicator_block.get("id") or None
        # Prefer the 3-letter ISO code supplied directly on the record.
        country: str | None = (
            raw.get("countryiso3code")
            or country_block.get("id")
            or None
        )
        period: str | None = str(raw.get("date") or "") or None

        raw_value = raw.get("value")
        value: float | None = None
        if raw_value is not None:
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                logger.debug(
                    "%s: cannot cast value %r to float for indicator %s",
                    self.source_id,
                    raw_value,
                    indicator_code,
                )

        # The WB 'unit' field exists on data-point records but is almost always
        # an empty string; we carry it verbatim so downstream can enrich it from
        # the indicator metadata if needed.
        unit: str = str(raw.get("unit") or "")

        return {
            "indicator": indicator_code,
            "country": country,
            "period": period,
            "value": value,
            "unit": unit,
        }


# ---------------------------------------------------------------------------
# Shared utility
# ---------------------------------------------------------------------------

def _parse_json_config(source_row: dict[str, Any]) -> dict[str, Any]:
    """Extract an optional JSON config dict from a source row.

    Checks ``source_row["config"]`` first, then ``source_row["notes"]``
    (which may carry a JSON string from a manual seed).  Returns ``{}`` when
    neither key contains a parseable JSON object.
    """
    for key in ("config", "notes"):
        raw = source_row.get(key)
        if not raw:
            continue
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                continue
    return {}
