"""UN Comtrade connector — lands annual trade-flow data into ``raw_trade_flows``.

Source:  https://comtradeapi.un.org/data/v1/get  (Comtrade+ REST API v1)
Auth:    ``Ocp-Apim-Subscription-Key`` request header, delivered via the
         envelope-encrypted ``connector_credentials`` store (credential ref
         ``cred/un_comtrade``).  The preview endpoint accepts keyless requests
         but returns heavily throttled data — a real key is required for
         production ingestion.
Quota:   Free preview tier ≈ 500 calls/day; premium subscription removes the
         per-day ceiling.  The connector throttles to one call every 2 s by
         default so short bursts stay within undocumented per-minute sub-limits.

Design — ``pull()`` iterates a Cartesian product of:

    * **HS codes** — collected from ``taxonomy_subcategories.hs_codes`` and
      normalised to Comtrade's dotless format (e.g. "8504.40" → "850440").
    * **Reporter / flow pairs** — mapped from the active geographies:
      - IMPORT segment → flowCode "M" (imports reported by that country)
      - EXPORT segment → flowCode "X" (exports reported by that country)
      - DOMESTIC segment is **skipped** — Comtrade records cross-border trade
        only, not domestic consumption.
    * **Calendar years** — from the ``since`` year (or a 3-year default
      lookback) to the current year.

Each API call requests the World aggregate partner (``partnerCode=0``) so a
single (reporter, cmdCode, flowCode, period) tuple produces at most a handful
of rows, well within the 500-record per-call maximum.  If partner-level detail
is ever needed in a future release, set ``partnerCode`` to individual country
codes and add pagination.

``normalize()`` maps one verbatim Comtrade data record to the typed columns
of ``raw_trade_flows``:

    reporter   ← reporterISO (ISO-3 alpha)
    partner    ← partnerISO / "WLD" for the World aggregate
    hs_code    ← cmdCode (exact code returned, e.g. "8541" or "850440")
    hs_version ← classificationSearchCode (edition the server resolved,
                  e.g. "H6"; stored so downstream can compare with requested)
    flow       ← flowCode ("M" = import, "X" = export, "MX" = both, etc.)
    period     ← "2023" (annual string)
    value_usd  ← primaryValue (USD)
    qty        ← netWgt (kg) when present, else altQty
    qty_unit   ← "kg" when netWgt used, else altQtyUnit

HS-code versioning:
    Requested with ``clCode`` = ``hs_version`` (default "H6", the 2022
    edition).  The *response* record carries ``classificationSearchCode``
    indicating which edition the API actually resolved — an earlier edition is
    returned automatically when H6 data is not yet available for a given
    reporter.  Both the raw_json and ``hs_version`` typed column capture the
    resolved edition.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Iterator

from connectors.base import (
    Connector,
    ProbeResult,
    classify_exception,
    classify_http_error,
)
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.comtrade")

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Canonical base URL for Comtrade+ REST API v1 (path suffixed per call).
_BASE_ENDPOINT = "https://comtradeapi.un.org/data/v1/get"

# Default HS classification edition. "HS" = the "as-reported / latest" view, which
# aggregates across editions and is what actually returns current annual data — the
# specific-edition endpoints (e.g. H6) can 500 or return empty for a given
# reporter/year. Override per-source via a `hs_version=H5` token in sources.notes.
_DEFAULT_HS_VERSION = "HS"

# Valid HS classification edition codes accepted by the API.
_VALID_HS_VERSIONS = frozenset({"HS", "H0", "H1", "H2", "H3", "H4", "H5", "H6"})

# Polite throttle between successive requests.
# Free tier: ~500 calls/day ≈ 0.006 req/s maximum; 2 s/call is safe against
# undocumented per-minute sub-limits while still completing a full scan within
# a reasonable pipeline window.
_MIN_INTERVAL = 2.0

# Default lookback when ``since`` is not supplied by the pipeline.
_DEFAULT_LOOKBACK_YEARS = 3

# ---------------------------------------------------------------------------
# Country → Comtrade reporter code mapping
#
# Comtrade uses UN M49 codes, which deviate from ISO 3166-1 numeric for a
# handful of economies (e.g. USA = 842 not 840; France = 251 not 250).
# Keys are stored in title-case; geography country names are normalised with
# str.title() before lookup.  Add rows here as the geography config expands.
# Reference: https://unstats.un.org/unsd/tradekb/Knowledgebase/Country-Code
# ---------------------------------------------------------------------------
_COUNTRY_TO_REPORTER: dict[str, int] = {
    # Asia-Pacific (primary geographies for the industrial engagement)
    "China": 156,
    "India": 699,       # UN Comtrade uses 699; ISO 3166-1 numeric is 356
    "Japan": 392,
    "Vietnam": 704,
    "South Korea": 410,
    "Korea, Republic Of": 410,
    "Taiwan": 490,      # Comtrade-specific; ISO does not assign a numeric code
    "Singapore": 702,
    "Malaysia": 458,
    "Thailand": 764,
    "Indonesia": 360,
    "Philippines": 608,
    "Hong Kong": 344,
    # Americas
    "United States": 842,
    "Usa": 842,
    "Mexico": 484,
    "Brazil": 76,
    "Canada": 124,
    # Europe
    "Germany": 276,
    "France": 251,      # Comtrade uses 251; ISO 3166-1 numeric is 250
    "United Kingdom": 826,
    "Italy": 380,
    "Netherlands": 528,
    "Belgium": 56,
    "Spain": 724,
    "Sweden": 752,
    "Switzerland": 756,
    "Austria": 40,
    "Poland": 616,
    "Czech Republic": 203,
    "Denmark": 208,
    "Finland": 246,
    "Norway": 578,
    # Oceania
    "Australia": 36,
}

# Geography segment → Comtrade flowCode.
# DOMESTIC is intentionally absent: Comtrade records cross-border trade only.
_SEGMENT_TO_FLOW: dict[str, str] = {
    "IMPORT": "M",
    "EXPORT": "X",
}


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions; no I/O)
# ---------------------------------------------------------------------------

def _normalise_hs_code(raw: str) -> str:
    """Strip the period separator used in taxonomy HS codes.

    Taxonomy YAML stores codes in human-readable form (e.g. "8504.40" for
    HS-6 subheading 40 of heading 8504).  Comtrade's ``cmdCode`` parameter
    expects the plain decimal string ("850440").  Four-digit heading codes
    like "8532" pass through unchanged.
    """
    return raw.replace(".", "").strip()


def _extract_hs_codes(taxonomy: list[dict[str, Any]]) -> list[str]:
    """Collect deduplicated, normalised HS codes from active taxonomy rows.

    ``taxonomy`` is the list of ``taxonomy_subcategories`` dicts the pipeline
    passes to ``pull()``.  Each row carries an ``hs_codes`` list of raw
    taxonomy strings.
    """
    seen: set[str] = set()
    codes: list[str] = []
    for sub in taxonomy:
        for raw in sub.get("hs_codes") or []:
            norm = _normalise_hs_code(str(raw))
            if norm and norm not in seen:
                seen.add(norm)
                codes.append(norm)
    return codes


def _lookback_years(since: str | None) -> list[str]:
    """Return an ordered list of year strings to query.

    When ``since`` is supplied as an ISO-8601 date string the start year is
    parsed from its first four characters.  When absent the default lookback
    applies.  The list always runs up to and including the current calendar
    year (Comtrade annual data typically lags 1–2 years, so recent years will
    return empty results — that is normal and is logged at DEBUG, not WARNING).
    """
    current_year = datetime.date.today().year
    if since:
        try:
            start_year = int(since[:4])
        except (ValueError, IndexError):
            start_year = current_year - _DEFAULT_LOOKBACK_YEARS
    else:
        start_year = current_year - _DEFAULT_LOOKBACK_YEARS
    return [str(y) for y in range(start_year, current_year + 1)]


def _build_reporter_pairs(
    geographies: list[dict[str, Any]],
) -> list[tuple[str, int, str]]:
    """Derive (country_name, reporter_code, flow_code) triples from geographies.

    DOMESTIC segments are skipped (no Comtrade equivalent).  Unknown countries
    are warned about once and skipped — never fabricating a reporter code.
    Duplicate (reporter_code, flow_code) pairs within a run are de-duplicated
    so a country appearing in multiple geography rows generates only one API
    call per flow direction.
    """
    pairs: list[tuple[str, int, str]] = []
    seen: set[tuple[int, str]] = set()
    warned_countries: set[str] = set()

    for geo in geographies:
        country: str = str(geo.get("country") or "")
        segment: str = str(geo.get("segment") or "").upper()

        flow_code = _SEGMENT_TO_FLOW.get(segment)
        if flow_code is None:
            # DOMESTIC or unrecognised segment — no Comtrade cross-border record.
            continue

        reporter_code = _COUNTRY_TO_REPORTER.get(country.title())
        if reporter_code is None:
            if country not in warned_countries:
                logger.warning(
                    "comtrade: no reporter code mapping for country %r — "
                    "skipping (add to _COUNTRY_TO_REPORTER to include it)",
                    country,
                )
                warned_countries.add(country)
            continue

        key = (reporter_code, flow_code)
        if key not in seen:
            seen.add(key)
            pairs.append((country, reporter_code, flow_code))

    return pairs


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

@register("comtrade")
class ComtradeConnector(Connector):
    """UN Comtrade annual trade-flow connector.

    Registered under the name ``"comtrade"`` matching the
    ``sources.connector`` value in ``config/sources.yaml``.

    The HS classification version defaults to ``H6`` (2022 edition) and can
    be overridden at the source-row level by embedding a ``hs_version=H5``
    token anywhere in ``sources.notes`` — useful for backfilling data in
    an older edition.
    """

    source_id = "un_comtrade"
    raw_table = "raw_trade_flows"
    min_interval = _MIN_INTERVAL

    def __init__(
        self,
        source_row: dict[str, Any],
        credential: str | None,
    ) -> None:
        super().__init__(source_row, credential)

        # Derive the API base URL from the seeded url_pattern when present
        # (makes the URL override-able without code changes).
        self._api_base: str = (
            str(self.base_url).rstrip("/")
            if self.base_url
            else _BASE_ENDPOINT
        )

        # Allow a ``hs_version=H5`` annotation in sources.notes to request a
        # specific edition without code changes (useful for backfills).
        self.hs_version: str = _DEFAULT_HS_VERSION
        notes: str = str(source_row.get("notes") or "")
        for token in notes.split():
            if token.startswith("hs_version="):
                _, _, ver = token.partition("=")
                if ver.upper() in _VALID_HS_VERSIONS:
                    self.hs_version = ver.upper()
                    logger.debug(
                        "comtrade: hs_version overridden to %s from source row notes",
                        self.hs_version,
                    )
                    break

    # ------------------------------------------------------------------
    # Auth headers
    # ------------------------------------------------------------------

    def default_headers(self) -> dict[str, str]:
        """Inject the Comtrade subscription key header when a credential exists.

        The header name ``Ocp-Apim-Subscription-Key`` is required by the Azure
        API Management gateway fronting Comtrade+.  Without it the gateway
        routes requests to the heavily throttled unauthenticated preview tier.
        """
        if self.credential:
            return {"Ocp-Apim-Subscription-Key": self.credential}
        return {}

    # ------------------------------------------------------------------
    # probe()
    # ------------------------------------------------------------------

    def probe(self) -> ProbeResult:
        """Cheap connectivity and authentication test.

        Issues a single minimal query — HS 8541 (discrete semiconductors) for
        Japan (reporter code 392), imports, prior calendar year, max 1 record —
        to verify the endpoint is reachable and the API key is accepted without
        consuming meaningful quota.  A 200 response with an empty data array
        is treated as ``OK`` rather than ``EMPTY``: annual data is released
        with a 1–2 year lag, so an empty result for the recent period does not
        indicate a source problem.
        """
        if self.missing_credential():
            return self.auth_failed_probe()

        # Comtrade annual data lags 1–2 years; use prior year for the probe.
        probe_year = str(datetime.date.today().year - 1)
        url = f"{self._api_base}/C/A/{self.hs_version}"
        params: dict[str, Any] = {
            "reporterCode": 392,        # Japan: reliable high-volume reporter
            "cmdCode": "8541",          # Discrete semiconductors (HS-4)
            "flowCode": "M",            # Imports
            "partnerCode": 0,           # World aggregate
            "partner2Code": 0,
            "customsCode": "C00",
            "motCode": "0",
            "period": probe_year,
            "maxRecords": 1,
            "format": "JSON",
            "breakdownMode": "classic",
            "includeDesc": "true",
        }

        try:
            resp = self.http.get(url, params=params)
        except Exception as exc:
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail)

        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception:
                return ProbeResult(
                    "SCHEMA_MISMATCH",
                    f"HTTP 200 but non-JSON body: {resp.text[:300]}",
                )

            data = body.get("data") or []
            if not data:
                return ProbeResult(
                    "OK",
                    f"HTTP 200; no records for {probe_year} "
                    f"(normal within the data-release lag window)",
                    None,
                )
            return ProbeResult(
                "OK",
                f"HTTP 200; {len(data)} record(s) returned (hs_version={self.hs_version})",
                data[:1],
            )

        status = classify_http_error(resp.status_code, resp.text)
        return ProbeResult(
            status,
            f"HTTP {resp.status_code}: {resp.text[:300]}",
        )

    # ------------------------------------------------------------------
    # pull()
    # ------------------------------------------------------------------

    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield verbatim Comtrade data records for the configured scope.

        Builds a Cartesian product of::

            HS codes × (reporter, flow) pairs × calendar years

        and issues one ``GET /data/v1/get/C/A/{hs_version}`` call per
        combination.  The World-aggregate partner (``partnerCode=0``) is used
        so each call returns the aggregate trade value — sufficient for market
        sizing and well within the 500-record per-call ceiling.

        Error handling:
            * ``QUOTA_EXHAUSTED`` / ``AUTH_FAILED`` → log at ERROR and abort
              the entire pull (further calls would also fail).
            * ``RATE_LIMITED`` → already handled by the shared HttpClient retry
              loop (with exponential backoff) before reaching here; logged if
              it persists after retries.
            * Other HTTP errors (``SCHEMA_MISMATCH``, ``UNREACHABLE``) → log
              the combination at WARNING and continue.
            * Transport errors → log and continue (best-effort).

        The connector NEVER yields fabricated rows — if a combination returns
        no data, the loop simply moves on.
        """
        if self.missing_credential():
            logger.warning(
                "comtrade: no credential available — pull() yields nothing "
                "(set credential in connector_credentials for source %r)",
                self.source_id,
            )
            return

        hs_codes = _extract_hs_codes(taxonomy)
        if not hs_codes:
            logger.warning(
                "comtrade: taxonomy contains no HS codes — nothing to pull"
            )
            return

        reporter_pairs = _build_reporter_pairs(geographies)
        if not reporter_pairs:
            logger.warning(
                "comtrade: no mappable reporter/flow pairs in geographies "
                "(all segments may be DOMESTIC, or no country codes are mapped)"
            )
            return

        years = _lookback_years(since)
        url = f"{self._api_base}/C/A/{self.hs_version}"
        total_calls = 0

        logger.info(
            "comtrade: pull starting — %d HS code(s), %d reporter/flow pair(s), "
            "%d year(s) → up to %d API calls",
            len(hs_codes),
            len(reporter_pairs),
            len(years),
            len(hs_codes) * len(reporter_pairs) * len(years),
        )

        for hs_code in hs_codes:
            for country, reporter_code, flow_code in reporter_pairs:
                for year in years:
                    params: dict[str, Any] = {
                        "reporterCode": reporter_code,
                        "cmdCode": hs_code,
                        "flowCode": flow_code,
                        "partnerCode": 0,           # World aggregate
                        "partner2Code": 0,
                        "customsCode": "C00",
                        "motCode": "0",
                        "period": year,
                        "maxRecords": 500,
                        "format": "JSON",
                        "breakdownMode": "classic",
                        "includeDesc": "true",
                    }

                    try:
                        resp = self.http.get(url, params=params)
                    except Exception as exc:
                        status, detail = classify_exception(exc)
                        logger.warning(
                            "comtrade: %s transport error for hs=%s "
                            "reporter=%s(%s) flow=%s year=%s — %s",
                            status, hs_code, country, reporter_code,
                            flow_code, year, detail,
                        )
                        continue

                    total_calls += 1

                    if resp.status_code != 200:
                        err_status = classify_http_error(
                            resp.status_code, resp.text
                        )
                        logger.warning(
                            "comtrade: %s HTTP %s for hs=%s reporter=%s(%s) "
                            "flow=%s year=%s: %s",
                            err_status, resp.status_code, hs_code, country,
                            reporter_code, flow_code, year, resp.text[:200],
                        )
                        if err_status in ("QUOTA_EXHAUSTED", "AUTH_FAILED"):
                            logger.error(
                                "comtrade: fatal %s after %d call(s) — "
                                "aborting pull(); update credential or wait "
                                "until quota resets",
                                err_status, total_calls,
                            )
                            return
                        continue

                    try:
                        body = resp.json()
                    except Exception as exc:
                        logger.warning(
                            "comtrade: HTTP 200 but non-JSON body for hs=%s "
                            "reporter=%s year=%s: %s",
                            hs_code, reporter_code, year, exc,
                        )
                        continue

                    records: list[dict[str, Any]] = body.get("data") or []
                    if not records:
                        logger.debug(
                            "comtrade: empty data for hs=%s reporter=%s(%s) "
                            "flow=%s year=%s (within data-lag window?)",
                            hs_code, country, reporter_code, flow_code, year,
                        )
                        continue

                    # Annotate each verbatim record with the requested
                    # classification version so normalize() can store it without
                    # needing call-context state.  This key is harmless in the
                    # JSONB raw payload and does not collide with Comtrade field
                    # names (which use camelCase).
                    for record in records:
                        record["_requested_hs_version"] = self.hs_version
                        yield record

                    logger.debug(
                        "comtrade: yielded %d record(s) — hs=%s reporter=%s(%s) "
                        "flow=%s year=%s",
                        len(records), hs_code, country, reporter_code,
                        flow_code, year,
                    )

                    # Q7 budget pre-warning: alert at ~80% of quota_ceiling.
                    warn = self.budget_warning(used_calls=total_calls)
                    if warn:
                        logger.warning(
                            "comtrade: quota pre-warning after %d call(s) — %s",
                            total_calls, warn,
                        )

        logger.info(
            "comtrade: pull complete — %d API call(s) issued", total_calls
        )

    # ------------------------------------------------------------------
    # normalize()
    # ------------------------------------------------------------------

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one verbatim Comtrade API record to ``raw_trade_flows`` columns.

        Returns a dict whose keys are a typed subset of the destination table.
        Extra keys in ``raw`` (Comtrade-internal fields, the injected
        ``_requested_hs_version``) are ignored — the pipeline stores the full
        record in ``raw_json`` verbatim.

        Field derivation
        ----------------
        reporter   : ``reporterISO`` (ISO-3 alpha string, e.g. "JPN") falling
                     back to ``reporterDesc`` when ISO is absent.
        partner    : ``partnerISO`` or "WLD" for the World aggregate
                     (``partnerCode`` == 0), falling back to ``partnerDesc``.
        hs_code    : ``cmdCode`` exactly as the API returned it (may differ
                     from the requested code when the server resolves to a
                     parent heading).
        hs_version : ``classificationSearchCode`` — the HS edition the server
                     actually resolved (e.g. "H6"), preferring this over the
                     injected ``_requested_hs_version`` because the resolved
                     edition is the authoritative one for the data row.
        flow       : ``flowCode`` ("M" = import, "X" = export, "MX" = both).
        period     : Annual period string as returned (e.g. "2023").
        value_usd  : ``primaryValue`` (USD); ``None`` when absent.
        qty        : ``netWgt`` (net weight, kg) when present, else ``altQty``;
                     ``None`` when neither is available.
        qty_unit   : "kg" when ``netWgt`` is used, else ``altQtyUnit``; ``None``
                     when qty is absent.
        """
        if not raw:
            return {}

        # Reporter: ISO-3 alpha preferred; descriptive name as fallback.
        reporter: str = str(
            raw.get("reporterISO") or raw.get("reporterDesc") or ""
        )

        # Partner: ISO-3 alpha preferred; "WLD" for World aggregate (code 0).
        partner_code = raw.get("partnerCode")
        partner: str = str(
            raw.get("partnerISO")
            or raw.get("partnerDesc")
            or ("WLD" if partner_code == 0 else "")
        )

        hs_code: str = str(raw.get("cmdCode") or "")

        # hs_version: prefer what the server resolved (authoritative); fall
        # back to what was requested (injected by pull()).
        hs_version: str = str(
            raw.get("classificationSearchCode")
            or raw.get("_requested_hs_version")
            or _DEFAULT_HS_VERSION
        )

        flow: str = str(raw.get("flowCode") or "")
        period: str = str(raw.get("period") or "")

        # value_usd: Comtrade primaryValue is denominated in USD.
        primary_val = raw.get("primaryValue")
        value_usd: float | None = (
            float(primary_val) if primary_val is not None else None
        )

        # Quantity: net weight (kg) is the most consistently populated field;
        # fall back to the commodity-specific alternate quantity.
        net_wgt = raw.get("netWgt")
        alt_qty = raw.get("altQty")
        if net_wgt is not None:
            qty: float | None = float(net_wgt)
            qty_unit: str | None = "kg"
        elif alt_qty is not None:
            qty = float(alt_qty)
            qty_unit = str(raw.get("altQtyUnit") or "") or None
        else:
            qty = None
            qty_unit = None

        return {
            "reporter": reporter,
            "partner": partner,
            "hs_code": hs_code,
            "hs_version": hs_version,
            "flow": flow,
            "period": period,
            "value_usd": value_usd,
            "qty": qty,
            "qty_unit": qty_unit,
        }
