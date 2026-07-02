"""USASpending.gov award-search connector.

Source: https://api.usaspending.gov/api/v2
Endpoint: POST /search/spending_by_award
Auth: none (entirely public)
Rate limit: ~1 000 calls / 5 min (we run at 0.4 s / request → ~150 req/min)
Raw table: raw_procurement

Maps to the ``tender_award_aggregation`` method (tier A) and feeds the
``raw_procurement`` table.  Awards (not solicitations) only; grants can be
pulled with a separate source row that sets ``award_type_codes`` to grant codes
via ``source_row["config"]``.

Invariants
----------
* Never fabricates data.  If required fields are absent in a record, the
  normalized column is set to None (not guessed).
* Pagination stops on the first page where ``page_metadata.hasNext = false``
  or when ``_MAX_PAGES`` is reached to cap per-run API spend.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from connectors.base import Connector, ProbeResult, classify_exception
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.usaspending")

_BASE = "https://api.usaspending.gov/api/v2"
_ENDPOINT = "/search/spending_by_award"
_PAGE_LIMIT = 100      # max records per page (API cap)
_MAX_PAGES = 50        # ceiling per pull run (~5 000 records max)

# Fields requested from the API; the key names in the ``results`` dicts match
# exactly what is listed here — USASpending uses labelled keys, not positional.
_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Awarding Agency Name",
    "Award Amount",
    "Start Date",
    "End Date",
    "Place of Performance Country Code",
    "Place of Performance State Code",
    "NAICS Code",
    "NAICS Description",
    "Award Type",
]

# Contract award-type codes (A–D are definitive / indefinite-delivery contracts).
# Exclude grants here so this connector is cleanly in the procurement lane.
_CONTRACT_CODES = ["A", "B", "C", "D"]


@register("usaspending")
class USASpendingConnector(Connector):
    """Connector for US Treasury / USASpending contract award data.

    Searches for awards via ``POST /api/v2/search/spending_by_award``.  When
    NAICS codes are present in the active taxonomy (stored as
    ``"NAICS:<code>"`` entries in ``regulatory_codes``), they are passed as
    a filter so results are scoped to the engagement's product categories.
    A broad date window is used otherwise.

    The connector writes one raw row per award record.  A single API result
    page (100 records) is a single batch; each record is yielded individually
    so the pipeline can decide on batch-insert sizing.

    Docs: https://api.usaspending.gov
    """

    source_id = "usaspending"
    raw_table = "raw_procurement"
    # Polite: 0.4 s ≈ 150 req/min, comfortably under the 200 req/min guidance.
    min_interval = 0.4

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _build_body(
        self,
        page: int,
        naics_codes: list[str] | None,
        since: str | None,
        end_date: str = "2025-12-31",
    ) -> dict[str, Any]:
        """Construct the POST body for one page of award results.

        Parameters
        ----------
        page:
            1-based page number.
        naics_codes:
            Optional NAICS codes to narrow the award search to the active
            taxonomy.  When None or empty, all contract awards are returned
            (subject to the date window).
        since:
            ISO date lookback start (``"YYYY-MM-DD"``).  Falls back to
            ``"2019-01-01"`` when not provided.
        end_date:
            Inclusive end of the time window.  Defaults to end of 2025.
        """
        start_date = since or "2019-01-01"
        filters: dict[str, Any] = {
            "award_type_codes": _CONTRACT_CODES,
            "time_period": [{"start_date": start_date, "end_date": end_date}],
        }
        if naics_codes:
            filters["naics_codes"] = {"require": naics_codes}

        return {
            "subawards": False,
            "filters": filters,
            "fields": _FIELDS,
            "page": page,
            "limit": _PAGE_LIMIT,
            "sort": "Award Amount",
            "order": "desc",
        }

    @staticmethod
    def _extract_naics(taxonomy: list[dict[str, Any]]) -> list[str]:
        """Extract NAICS codes from active taxonomy subcategory rows.

        The industrial reference engagement stores NAICS codes in the
        ``regulatory_codes`` array as ``"NAICS:<six-digit-code>"`` entries.
        Up to 10 codes are forwarded to the API filter (practical limit on
        filter cardinality for government search APIs).
        """
        codes: list[str] = []
        seen: set[str] = set()
        for row in taxonomy:
            for code in row.get("regulatory_codes") or []:
                if not isinstance(code, str):
                    continue
                if code.upper().startswith("NAICS:"):
                    naics = code[6:].strip()
                    if naics and naics not in seen:
                        seen.add(naics)
                        codes.append(naics)
                        if len(codes) >= 10:
                            return codes
        return codes

    # ------------------------------------------------------------------ #
    # probe
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """Fire a single-record test call to verify the endpoint is reachable.

        Uses a minimal body (1 record, no NAICS filter, recent date range) to
        stay cheap and fast.
        """
        body = self._build_body(page=1, naics_codes=None, since="2024-01-01")
        body["limit"] = 1  # cheapest possible probe

        try:
            resp = self.http.request("POST", f"{_BASE}{_ENDPOINT}", json=body)
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail, None)

        if resp.status_code >= 300:
            return self.probe_via(resp)

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return ProbeResult(
                "SCHEMA_MISMATCH",
                f"HTTP {resp.status_code} but response is not valid JSON",
                None,
            )

        results: list[Any] = data.get("results") or []
        meta: dict[str, Any] = data.get("page_metadata") or {}
        total = meta.get("total", "?")

        if not results:
            return ProbeResult(
                "EMPTY",
                f"API reachable but zero results (total={total})",
                None,
            )

        return ProbeResult(
            "OK",
            f"HTTP 200; {total} total awards indexed",
            results[0],
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
        """Yield verbatim USASpending award records (one dict per award).

        Each yielded dict is the raw API result record (field names include
        spaces, matching the ``fields`` list), augmented with ``_page`` for
        traceability.  The pipeline stores it verbatim as ``raw_json``.

        Pagination follows ``page_metadata.hasNext``; stops early at
        ``_MAX_PAGES`` to cap per-run API spend.
        """
        naics_codes = self._extract_naics(taxonomy)
        if naics_codes:
            logger.info(
                "%s: filtering by %d NAICS code(s): %s",
                self.source_id, len(naics_codes), naics_codes,
            )
        else:
            logger.info(
                "%s: no NAICS codes in taxonomy — pulling broad contract award set",
                self.source_id,
            )

        page = 1
        while page <= _MAX_PAGES:
            body = self._build_body(page, naics_codes, since)
            try:
                resp = self.http.request("POST", f"{_BASE}{_ENDPOINT}", json=body)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "%s: request failed on page %d: %s", self.source_id, page, exc
                )
                return

            if resp.status_code >= 300:
                logger.info(
                    "%s: HTTP %s on page %d; stopping pull",
                    self.source_id, resp.status_code, page,
                )
                return

            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "%s: non-JSON response on page %d", self.source_id, page
                )
                return

            results: list[dict[str, Any]] = data.get("results") or []
            if not results:
                logger.info("%s: empty results on page %d; stopping", self.source_id, page)
                return

            for record in results:
                # Shallow-copy so we don't mutate the parsed response object.
                tagged = dict(record)
                tagged["_page"] = page
                yield tagged

            meta: dict[str, Any] = data.get("page_metadata") or {}
            if not meta.get("hasNext", False):
                logger.info(
                    "%s: last page reached (page=%d, total=%s)",
                    self.source_id, page, meta.get("total", "?"),
                )
                return

            page += 1

        logger.info(
            "%s: hit _MAX_PAGES=%d ceiling; stopping to preserve API quota",
            self.source_id, _MAX_PAGES,
        )

    # ------------------------------------------------------------------ #
    # normalize
    # ------------------------------------------------------------------ #

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one verbatim USASpending award record to raw_procurement columns.

        Column mapping
        --------------
        award_id  <- "Award ID" (PIID / grant number)
        buyer     <- "Awarding Agency Name"
        supplier  <- "Recipient Name"
        country   <- "Place of Performance Country Code" (ISO-3 or "USA")
        value_usd <- "Award Amount" (cast to float; None on cast failure)
        period    <- "YYYY-MM/YYYY-MM" from "Start Date" / "End Date"
        """
        award_id: str | None = (
            raw.get("Award ID")
            or raw.get("generated_internal_id")
        )
        buyer: str | None = raw.get("Awarding Agency Name")
        supplier: str | None = raw.get("Recipient Name")
        country: str | None = raw.get("Place of Performance Country Code")

        raw_amount = raw.get("Award Amount")
        value_usd: float | None = None
        if raw_amount is not None:
            try:
                value_usd = float(raw_amount)
            except (TypeError, ValueError):
                logger.debug(
                    "%s: could not cast 'Award Amount'=%r to float",
                    self.source_id, raw_amount,
                )

        start = (raw.get("Start Date") or "")[:7]  # "YYYY-MM"
        end = (raw.get("End Date") or "")[:7]
        period: str | None = None
        if start and end:
            period = f"{start}/{end}"
        elif start:
            period = start

        return {
            "award_id": award_id,
            "buyer": buyer,
            "supplier": supplier,
            "country": country,
            "value_usd": value_usd,
            "period": period,
        }
