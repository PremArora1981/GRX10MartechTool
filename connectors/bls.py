"""BLS Public Data API v2 connector — lands data into ``raw_signals``.

Source
------
Endpoint : POST https://api.bls.gov/publicAPI/v2/timeseries/data/
Auth     : ``registrationkey`` request body field (free key from bls.gov).
Class    : C — hiring / labour-market signals; triangulation support only.
Rate     : 500 queries/day, 50 series per query (registered key).

Series scope
------------
This connector pulls **JOLTS** (Job Openings and Labor Turnover Survey) series.
JOLTS series IDs are 21 characters and encode sector, area, and item codes:

    ``JTS000000000000000JOR``
    └─┘└─────────────────┘└─┘
    JTS   14-char codes     item

Default series (total nonfarm, seasonally adjusted)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    JTS000000000000000JOR — Job openings rate
    JTS000000000000000HIR — Hires rate
    JTS000000000000000QUR — Quits rate
    JTS000000000000000TSR — Total separations rate
    JTS000000000000000LDR — Layoffs and discharges rate
    JTS000000000000000JOL — Job openings level (thousands)
    JTS000000000000000HIL — Hires level (thousands)

Override by encoding a JSON object in the ``notes`` column of the matching
``sources`` row:

    {"series": ["JTS3000000000000000JOR", "JTS3000000000000000HIR"]}

The ``company`` column in ``raw_signals`` is repurposed here to hold the
**sector / series label** (e.g. ``"Total Nonfarm"``), since BLS provides
aggregate sector data, not company-level data.

Design notes
------------
* pull() yields one dict **per data point** (one year+period observation per
  series), not per API response page.  This keeps normalize() a trivial 1-to-1
  mapping and the raw_json small and inspectable.
* BLS v2 requires a POST body with ``seriesid`` (array), ``startyear``,
  ``endyear``, and optionally ``registrationkey``.
* The BLS API caps to 20 years of data per request; when the lookback exceeds
  20 years we issue multiple requests with non-overlapping year windows.
* Without a key, the daily cap is lower; with a key it is 500 queries/day with
  50 series per query.  We throttle conservatively at 2 s between requests even
  though no per-second limit is published.
"""

from __future__ import annotations

import calendar
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Iterator

from connectors.base import Connector, ProbeResult, classify_exception, classify_http_error
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.bls")

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

_MAX_SERIES_PER_REQUEST = 50       # BLS hard limit (registered key)
_MAX_YEAR_SPAN = 20                # BLS hard limit per request
_DEFAULT_LOOKBACK_YEARS = 5        # used when caller passes since=None

# Item-code suffix (last 3 chars of series ID) → human-readable signal_type.
_ITEM_CODE_TO_TYPE: dict[str, str] = {
    "JOL": "job_openings_level",
    "JOR": "job_openings_rate",
    "HIL": "hires_level",
    "HIR": "hires_rate",
    "QUL": "quits_level",
    "QUR": "quits_rate",
    "TSL": "total_separations_level",
    "TSR": "total_separations_rate",
    "LDL": "layoffs_discharges_level",
    "LDR": "layoffs_discharges_rate",
    "OSL": "other_separations_level",
    "OSR": "other_separations_rate",
}

# BLS period code (M01–M12) → zero-padded month string.
_PERIOD_TO_MONTH: dict[str, str] = {f"M{i:02d}": f"{i:02d}" for i in range(1, 13)}
# Annual series use M13 (annual average).

# Default JOLTS series (total nonfarm, seasonally adjusted).
_DEFAULT_SERIES: list[str] = [
    "JTS000000000000000JOR",  # job openings rate
    "JTS000000000000000HIR",  # hires rate
    "JTS000000000000000QUR",  # quits rate
    "JTS000000000000000TSR",  # total separations rate
    "JTS000000000000000LDR",  # layoffs and discharges rate
    "JTS000000000000000JOL",  # job openings level (thousands)
    "JTS000000000000000HIL",  # hires level (thousands)
]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _series_from_source_row(notes: str | None) -> list[str]:
    """Parse optional JSON series config from the ``sources.notes`` column.

    Expects ``{"series": ["SER001", "SER002"]}`` if present.  Falls back to
    ``_DEFAULT_SERIES`` when ``notes`` is absent or is plain text.
    """
    if not notes:
        return list(_DEFAULT_SERIES)
    notes = notes.strip()
    if not notes.startswith("{"):
        return list(_DEFAULT_SERIES)
    try:
        cfg = json.loads(notes)
        ids = cfg.get("series") or []
        if isinstance(ids, list) and all(isinstance(s, str) for s in ids):
            return ids or list(_DEFAULT_SERIES)
    except (json.JSONDecodeError, TypeError):
        pass
    logger.debug("bls: could not parse series config from notes; using defaults")
    return list(_DEFAULT_SERIES)


def _year_windows(start_year: int, end_year: int) -> list[tuple[int, int]]:
    """Split [start_year, end_year] into ≤20-year windows for BLS API compliance."""
    windows: list[tuple[int, int]] = []
    year = start_year
    while year <= end_year:
        window_end = min(year + _MAX_YEAR_SPAN - 1, end_year)
        windows.append((year, window_end))
        year = window_end + 1
    return windows


def _signal_type_from_series_id(series_id: str) -> str:
    """Derive a readable signal_type from the last 3 chars of the series ID."""
    if len(series_id) >= 3:
        code = series_id[-3:].upper()
        return _ITEM_CODE_TO_TYPE.get(code, f"bls_{code.lower()}")
    return "bls_unknown"


def _format_period(year: str, period: str) -> str:
    """Convert BLS year + period code to 'YYYY-MM' (or 'YYYY' for annual)."""
    month = _PERIOD_TO_MONTH.get(period)
    if month:
        return f"{year}-{month}"
    # M13 = annual average; S01/S02 = semi-annual; Q01-Q05 = quarterly.
    if period == "M13":
        return str(year)
    # Semi-annual / quarterly: store as reported.
    return f"{year}-{period}"


def _check_bls_status(data: dict[str, Any]) -> str | None:
    """Return an error string if the BLS response carries a non-success status.

    BLS returns HTTP 200 even for auth/quota failures; the real status lives in
    ``data["status"]`` (values: ``REQUEST_SUCCEEDED``, ``REQUEST_FAILED_ERROR``,
    ``REQUEST_NOT_PROCESSED``).
    """
    status = (data.get("status") or "").upper()
    if status == "REQUEST_SUCCEEDED":
        return None
    messages = data.get("message") or []
    msg_text = "; ".join(str(m) for m in messages) if messages else "no detail"
    return f"BLS status={status!r}: {msg_text}"


@register("bls")
class BLSConnector(Connector):
    """Connector for the US Bureau of Labor Statistics Public Data API v2.

    Pulls JOLTS (Job Openings and Labor Turnover Survey) time-series data into
    ``raw_signals`` for use by the ``hiring_capacity_proxy`` estimation method.

    Expected ``sources`` row values
    --------------------------------
    source_id      : ``bls_publicdata_api_v2``
    url_pattern    : ``https://api.bls.gov/publicAPI/v2/timeseries/data/``
                     (or None — the connector has a hard-coded fallback)
    auth           : ``api_key``
    auth_secret_ref: pointer to the BLS registration key
    raw_table      : ``raw_signals``
    class          : ``C``
    notes          : (optional) JSON: ``{"series": [...]}``
    """

    source_id: str = "bls_publicdata_api_v2"
    raw_table: str = "raw_signals"

    # Conservative: no documented per-second limit, but 500 queries/day.
    # 2 s between requests keeps us well under 500/day even in heavy use.
    min_interval: float = 2.0

    # ── private helpers ─────────────────────────────────────────────────────

    def _endpoint(self) -> str:
        return (self.base_url or _API_URL).rstrip("/")

    def _build_body(
        self,
        series_ids: list[str],
        start_year: int,
        end_year: int,
    ) -> dict[str, Any]:
        """Assemble the BLS v2 POST body."""
        body: dict[str, Any] = {
            "seriesid": series_ids,
            "startyear": str(start_year),
            "endyear": str(end_year),
            "calculations": False,
            "annualaverage": False,
        }
        if self.credential:
            body["registrationkey"] = self.credential
        return body

    # ── Connector contract ───────────────────────────────────────────────────

    def probe(self) -> ProbeResult:
        """POST a single-series request to verify auth and API reachability.

        Uses the first default JOLTS series for the most recent calendar year.
        Note: BLS always returns HTTP 200; the real auth/quota status is in
        the JSON body's ``status`` field, which we inspect here.
        """
        if self.missing_credential():
            return self.auth_failed_probe()

        current_year = datetime.now(timezone.utc).year
        body = self._build_body(
            series_ids=[_DEFAULT_SERIES[0]],
            start_year=current_year - 1,
            end_year=current_year,
        )

        try:
            resp = self.http.post(self._endpoint(), json=body)
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail)

        if resp.status_code != 200:
            status = classify_http_error(resp.status_code, resp.text)
            return ProbeResult(status, f"HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return ProbeResult("SCHEMA_MISMATCH",
                               f"200 OK but JSON parse failed: {resp.text[:200]}")

        err = _check_bls_status(data)
        if err:
            # Map common BLS error patterns to the 7-state taxonomy.
            err_lower = err.lower()
            if "invalid" in err_lower and "key" in err_lower:
                return ProbeResult("AUTH_FAILED", err)
            if "exceeded" in err_lower or "limit" in err_lower or "quota" in err_lower:
                return ProbeResult("QUOTA_EXHAUSTED", err)
            return ProbeResult("SCHEMA_MISMATCH", err)

        series_list = (data.get("Results") or {}).get("series") or []
        if not series_list:
            return ProbeResult("EMPTY", "authenticated but 0 series in probe response", None)

        first_series_data = (series_list[0].get("data") or [])[:1]
        return ProbeResult(
            "OK",
            f"HTTP 200, BLS status=REQUEST_SUCCEEDED; "
            f"{len(first_series_data)} point(s) for {_DEFAULT_SERIES[0]}",
            first_series_data,
        )

    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield one verbatim data-point dict per BLS observation (year+period).

        ``taxonomy`` and ``geographies`` are accepted by the contract but not
        used for API-side filtering — JOLTS provides aggregate US national data
        and cannot be filtered by HS taxonomy.  Use the ``hiring_capacity_proxy``
        method to scale to specific geographies.

        ``since`` sets the lookback start year.  When ``None``, defaults to the
        last :data:`_DEFAULT_LOOKBACK_YEARS` years.
        """
        if self.missing_credential():
            logger.warning("%s: no credential — yielding nothing", self.source_id)
            return

        # Determine year range.
        current_year = datetime.now(timezone.utc).year
        if since:
            try:
                start_year = int(since[:4])
            except (ValueError, TypeError):
                start_year = current_year - _DEFAULT_LOOKBACK_YEARS
        else:
            start_year = current_year - _DEFAULT_LOOKBACK_YEARS
        end_year = current_year

        # Get series IDs from source row configuration or defaults.
        notes = self.source_row.get("notes")
        series_ids = _series_from_source_row(notes)
        logger.info(
            "%s: pulling %d JOLTS series; years %d–%d",
            self.source_id, len(series_ids), start_year, end_year,
        )

        # Batch series into groups of ≤50 per BLS limit.
        n_batches = math.ceil(len(series_ids) / _MAX_SERIES_PER_REQUEST)
        series_batches = [
            series_ids[i * _MAX_SERIES_PER_REQUEST:(i + 1) * _MAX_SERIES_PER_REQUEST]
            for i in range(n_batches)
        ]

        # BLS caps each request to 20 years; split into windows if needed.
        year_windows = _year_windows(start_year, end_year)

        for batch in series_batches:
            for win_start, win_end in year_windows:
                body = self._build_body(batch, win_start, win_end)

                try:
                    resp = self.http.post(self._endpoint(), json=body)
                except Exception as exc:  # noqa: BLE001
                    status, detail = classify_exception(exc)
                    logger.error(
                        "%s: transport error for window %d-%d — %s: %s",
                        self.source_id, win_start, win_end, status, detail,
                    )
                    return

                if resp.status_code != 200:
                    status = classify_http_error(resp.status_code, resp.text)
                    logger.warning(
                        "%s: HTTP %d for window %d-%d — %s",
                        self.source_id, resp.status_code, win_start, win_end, status,
                    )
                    return

                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    logger.error(
                        "%s: JSON decode failed for window %d-%d",
                        self.source_id, win_start, win_end,
                    )
                    return

                err = _check_bls_status(data)
                if err:
                    logger.warning("%s: %s", self.source_id, err)
                    # Do not abort the entire pull on a single failing window —
                    # quota errors might affect one window but not another.
                    if "invalid" in err.lower() and "key" in err.lower():
                        return  # auth failure: abort entirely
                    continue

                series_results = (data.get("Results") or {}).get("series") or []
                for series_obj in series_results:
                    series_id: str = series_obj.get("seriesID") or ""
                    data_points: list[dict[str, Any]] = series_obj.get("data") or []
                    for point in data_points:
                        # Embed the series_id into the point so normalize() has
                        # full context without needing the parent object.
                        yield {"_series_id": series_id, **point}

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one verbatim BLS data point to ``raw_signals`` typed columns.

        Columns produced
        ----------------
        company     : series label derived from series ID (sector / item description).
                      BLS JOLTS provides aggregate US data, not company data; this
                      column carries the sector/item code (e.g. ``"JOR_total_nonfarm"``).
        signal_type : human-readable item code (e.g. ``"job_openings_rate"``).
        country     : ``"US"`` (JOLTS covers US labour market only).
        period      : ISO-style period string, e.g. ``"2024-03"`` or ``"2024"``
                      (annual averages).
        value       : numeric value as reported (rate in %; level in thousands).
        """
        series_id: str = str(raw.get("_series_id") or "")
        year: str = str(raw.get("year") or "")
        period_code: str = str(raw.get("period") or "")
        raw_value: str | None = raw.get("value")

        signal_type = _signal_type_from_series_id(series_id)

        # Compose a "company" label for the sector.  For JOLTS aggregate series
        # the first 18 chars after "JTS" are all zeros → "total_nonfarm".
        # For sector-specific series (e.g. manufacturing starts with 3) the
        # label would need a lookup table; use the series ID stem as a safe
        # fallback for non-aggregate series.
        sector_stem = series_id[3:-3] if len(series_id) == 21 else series_id
        if all(c == "0" for c in sector_stem):
            sector_label = "total_nonfarm"
        else:
            sector_label = f"sector_{sector_stem}"
        company = f"{series_id[-3:].lower()}_{sector_label}"  # e.g. "jor_total_nonfarm"

        # Parse value: BLS occasionally uses "-" for suppressed cells.
        value: float | None = None
        if raw_value not in (None, "", "-", "N/A", "nan"):
            try:
                value = float(raw_value)
            except (ValueError, TypeError):
                value = None

        period_str = _format_period(year, period_code) if year else None

        return {
            "company": company,
            "signal_type": signal_type,
            "country": "US",
            "period": period_str,
            "value": value,
        }
