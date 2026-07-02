"""US Census Bureau International Trade Statistics connector.

Source ID  : us_census_intltrade
Raw table  : raw_trade_flows
Auth       : api_key  (free, self-service — https://api.census.gov/data/key_signup.html)
Class      : A — authoritative for US-origin/destination trade flows

API endpoint families
---------------------
  timeseries/intltrade/imports/hs  — monthly general + consumption imports by HTS-10
  timeseries/intltrade/exports/hs  — monthly total exports by Schedule-B 10 + country

The Census trade API returns an **array-of-arrays** where row 0 is the column-name
header and rows 1+ are data.  Both endpoints are queried for every HS-6 code present
in the active taxonomy.

HS-code handling
----------------
The taxonomy stores HS-6 codes (6 digits).  Census uses 10-digit HTS (imports) and
Schedule-B (exports) codes.  This connector right-pads HS-6 with four zeros
(``850440`` → ``8504400000``) to query the dominant heading.  The verbatim 10-digit
code from the API response is stored in ``raw_json`` and reflected in the typed
``hs_code`` column; ``hs_version`` is set to ``"HTS10"`` to mark the lineage so
downstream methods can normalise to HS-6 by slicing ``[:6]``.

Rate limits
-----------
~500 req/day without a key; ~500 req/hour with a registered free key.  Default polite
spacing is 1 second between requests.  The ``since`` pull parameter drives the start
month; defaults to January of the prior complete year when absent.
"""

from __future__ import annotations

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

logger = logging.getLogger("grx10.connectors.census_intltrade")

_IMPORTS_URL = "https://api.census.gov/data/timeseries/intltrade/imports/hs"
_EXPORTS_URL = "https://api.census.gov/data/timeseries/intltrade/exports/hs"

# Comma-separated field lists for each endpoint.
# GEN_VAL_MO = general imports monthly value (USD); CON_VAL_MO = consumption value.
# ALL_VAL_MO  = total exports monthly value (USD).
_IMPORT_FIELDS = "GEN_VAL_MO,CON_VAL_MO,I_COMMODITY,CTY_CODE,CTY_NAME"
_EXPORT_FIELDS = "ALL_VAL_MO,E_COMMODITY,CTY_CODE,CTY_NAME"

# Well-known code for probe (capacitors / power converters — HS heading 8504.40)
_PROBE_COMMODITY = "8504400000"
_PROBE_PERIOD = "2023-01"


@register("census_intltrade")
class CensusIntlTradeConnector(Connector):
    """US Census International Trade (imports + exports) → raw_trade_flows.

    Each Census API row becomes one verbatim dict yielded by :meth:`pull` and
    stored in ``raw_trade_flows.raw_json``.  :meth:`normalize` maps it to the
    typed columns (reporter, partner, hs_code, hs_version, flow, period, value_usd).
    """

    source_id = "us_census_intltrade"
    raw_table = "raw_trade_flows"
    min_interval = 1.0  # polite; documented ceiling is ~500/hr with key

    # ------------------------------------------------------------------ #
    # Probe                                                                #
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """Cheap health check: one imports query for a known HTS-10 code.

        Returns ``AUTH_FAILED`` immediately when no API key is available (the key
        is optional but highly recommended for production use).
        """
        if self.missing_credential():
            return self.auth_failed_probe()

        params: dict[str, str] = {
            "get": "GEN_VAL_MO,I_COMMODITY,CTY_CODE",
            "I_COMMODITY": _PROBE_COMMODITY,
            "time": _PROBE_PERIOD,
            "key": self.credential,  # type: ignore[assignment]
        }
        try:
            resp = self.http.get(_IMPORTS_URL, params=params)
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail)

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                return ProbeResult("SCHEMA_MISMATCH", "response is not valid JSON")
            if not isinstance(data, list) or len(data) < 2:
                # Header-only response means the query returned no matching rows.
                return ProbeResult(
                    "EMPTY", f"array-of-arrays had {len(data)} element(s), need ≥2"
                )
            return ProbeResult("OK", f"HTTP 200; {len(data) - 1} data row(s)", data[1])

        status = classify_http_error(resp.status_code, resp.text)
        return ProbeResult(status, f"HTTP {resp.status_code}: {resp.text[:200]}")

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
        """Yield one dict per Census API data row (verbatim + ``_flow`` key).

        Queries both imports and exports for every HS-6 code found in *taxonomy*.
        The ``since`` ISO-date hint drives the lookback start month (``YYYY-MM``);
        absent or unparseable values default to January of the prior complete year.

        The pipeline stores the yielded dicts verbatim as ``raw_trade_flows.raw_json``
        before calling :meth:`normalize` for the typed columns.
        """
        if self.missing_credential():
            logger.warning("census_intltrade: no API key — skipping pull")
            return

        hs_codes = _extract_hs_codes(taxonomy)
        if not hs_codes:
            logger.info("census_intltrade: taxonomy has no HS codes — nothing to pull")
            return

        start_period = _parse_since_to_period(since)
        end_period = _current_period()

        logger.info(
            "census_intltrade: pulling %d HS code(s) from %s to %s",
            len(hs_codes), start_period, end_period,
        )

        for hs6 in sorted(hs_codes):
            hts10 = _hs6_to_hts10(hs6)
            yield from self._pull_endpoint(
                url=_IMPORTS_URL,
                commodity_param="I_COMMODITY",
                fields=_IMPORT_FIELDS,
                commodity_code=hts10,
                start_period=start_period,
                end_period=end_period,
                flow="IMPORT",
            )
            yield from self._pull_endpoint(
                url=_EXPORTS_URL,
                commodity_param="E_COMMODITY",
                fields=_EXPORT_FIELDS,
                commodity_code=hts10,
                start_period=start_period,
                end_period=end_period,
                flow="EXPORT",
            )

    def _pull_endpoint(
        self,
        *,
        url: str,
        commodity_param: str,
        fields: str,
        commodity_code: str,
        start_period: str,
        end_period: str,
        flow: str,
    ) -> Iterator[dict[str, Any]]:
        """Request one Census endpoint + commodity and yield verbatim row dicts.

        Uses the Census timeseries range syntax ``from YYYY-MM to YYYY-MM`` to
        retrieve all months in a single call.  On any failure the error is logged
        and the method yields nothing rather than raising (pipeline-safe).
        """
        params: dict[str, str] = {
            "get": fields,
            commodity_param: commodity_code,
            # Census timeseries range: "from YYYY-MM to YYYY-MM"
            "time": f"from {start_period} to {end_period}",
            "key": self.credential or "",
        }

        try:
            resp = self.http.get(url, params=params)
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            logger.warning(
                "census_intltrade %s %s: %s — %s", flow, commodity_code, status, detail
            )
            return

        if resp.status_code != 200:
            status = classify_http_error(resp.status_code, resp.text)
            logger.warning(
                "census_intltrade %s %s: HTTP %d (%s): %s",
                flow, commodity_code, resp.status_code, status, resp.text[:120],
            )
            return

        try:
            data = resp.json()
        except Exception:
            logger.warning(
                "census_intltrade %s %s: non-JSON response (len=%d)",
                flow, commodity_code, len(resp.content),
            )
            return

        if not isinstance(data, list) or len(data) < 2:
            logger.debug(
                "census_intltrade %s %s: empty result (%d elements)",
                flow, commodity_code, len(data),
            )
            return

        # data[0] = header row (field names); data[1:] = data rows
        headers = [h.upper() for h in data[0]]
        for row in data[1:]:
            row_dict = dict(zip(headers, row))
            # Inject flow direction so normalize() doesn't have to guess
            row_dict["_flow"] = flow
            yield row_dict

    # ------------------------------------------------------------------ #
    # Normalize                                                            #
    # ------------------------------------------------------------------ #

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one Census array-of-arrays row dict to raw_trade_flows typed columns.

        The full verbatim row (including the injected ``_flow`` key) is stored in
        ``raw_trade_flows.raw_json`` by the pipeline; this method derives only the
        typed columns.

        Reporter is always ``"US"`` — Census data covers US trade only.
        Values are in **USD** as reported by the Census Bureau.
        """
        flow = raw.get("_flow", "IMPORT")

        if flow == "IMPORT":
            # Prefer general imports value; fall back to consumption imports
            value_usd = _to_numeric(
                raw.get("GEN_VAL_MO") or raw.get("CON_VAL_MO")
            )
            commodity = str(raw.get("I_COMMODITY") or "")
        else:
            value_usd = _to_numeric(raw.get("ALL_VAL_MO"))
            commodity = str(raw.get("E_COMMODITY") or "")

        # CTY_NAME is the human-readable partner name; fall back to numeric code
        partner = raw.get("CTY_NAME") or raw.get("CTY_CODE")

        # Period comes through as "TIME" (upper-cased header) or raw "time"
        period = raw.get("TIME") or raw.get("time")

        # Truncate 10-digit HTS/Schedule-B to HS-6 for the typed column;
        # the full 10-digit code is preserved in raw_json.
        hs6 = commodity[:6] if len(commodity) >= 6 else (commodity or None)

        return {
            "reporter": "US",
            "partner": partner,
            "hs_code": hs6,
            "hs_version": "HTS10",   # US Harmonized Tariff Schedule 10-digit source
            "flow": flow,
            "period": period,
            "value_usd": value_usd,
            # Monthly Census totals aggregate quantity across all sub-quantities;
            # quantity detail is not returned by this endpoint.
            "qty": None,
            "qty_unit": None,
        }


# ------------------------------------------------------------------ #
# Module-level helpers                                                #
# ------------------------------------------------------------------ #

def _extract_hs_codes(taxonomy: list[dict[str, Any]]) -> set[str]:
    """Collect unique HS-6 prefixes from all taxonomy_subcategories rows.

    The ``hs_codes`` column is a Postgres TEXT[] array; the pipeline delivers
    it as a Python list.  Codes are trimmed of dots and spaces, then capped at
    6 digits.
    """
    codes: set[str] = set()
    for row in taxonomy:
        for code in row.get("hs_codes") or []:
            cleaned = str(code).strip().replace(".", "").replace(" ", "")
            if cleaned:
                codes.add(cleaned[:6])
    return codes


def _hs6_to_hts10(hs6: str) -> str:
    """Right-pad a 6-digit HS code to 10 digits with zeros (HTS/Schedule-B style).

    Example: ``"850440"`` → ``"8504400000"``.
    """
    return hs6.ljust(10, "0")[:10]


def _parse_since_to_period(since: str | None) -> str:
    """Convert an ISO-date lookback hint to a ``YYYY-MM`` Census period string.

    Falls back to ``{prior_year}-01`` when *since* is absent or unparseable,
    covering the full prior calendar year.
    """
    if since:
        try:
            dt = datetime.fromisoformat(since.split("T")[0])
            return dt.strftime("%Y-%m")
        except ValueError:
            pass
    prior = datetime.now(timezone.utc).year - 1
    return f"{prior}-01"


def _current_period() -> str:
    """Return the most recent complete month as ``YYYY-MM``.

    Backs off one month from today to avoid requesting an incomplete
    current-month value.
    """
    now = datetime.now(timezone.utc)
    if now.month == 1:
        return f"{now.year - 1}-12"
    return f"{now.year}-{now.month - 1:02d}"


def _to_numeric(value: Any) -> float | None:
    """Coerce a Census string value to float; returns ``None`` for blanks/nulls."""
    if value is None:
        return None
    s = str(value).strip()
    if s in ("", "null", "N", "-", "D"):  # D = suppressed/withheld by Census
        return None
    try:
        return float(s.replace(",", ""))
    except (ValueError, TypeError):
        return None
