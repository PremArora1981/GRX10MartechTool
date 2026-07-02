"""openFDA connector — device registration listings and drug NDC records.

Lands data into ``raw_regulatory`` with typed columns:
    registration_id / holder / product_code / country / status

Source details (connector-catalog §2):
    source_id   : openfda
    raw_table   : raw_regulatory
    class       : A
    auth        : optional API key (credential store or env FDA_API_KEY);
                  keyless works at 40 req/min; keyed at 240 req/min.
    base_url    : https://api.fda.gov
    endpoints   : device/registrationlisting.json
                  drug/ndc.json
    pagination  : ?limit / ?skip  (hard cap: skip ≤ 25 000 per query)

Known limitation: openFDA caps pagination at skip = 25 000, so at most ~26 000
records can be retrieved per endpoint per run.  For full device-master coverage,
pair this connector with ``fda_accessgudid`` (weekly bulk ZIP delta).

Both endpoints are pulled on every ``pull()`` call.  The ``_endpoint`` key
injected into every yielded record is used by ``normalize()`` to dispatch the
correct field mapping; it is stored verbatim in ``raw_json`` alongside the
original payload (harmless extra key in JSONB).
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from connectors.base import (
    Connector,
    ProbeResult,
    classify_exception,
    classify_http_error,
)
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.openfda")

_BASE_URL = "https://api.fda.gov"
_DEVICE_EP = "device/registrationlisting.json"
_DRUG_EP = "drug/ndc.json"

# openFDA hard limits per request / per skip offset.
_PAGE_SIZE = 1_000
_SKIP_CAP = 25_000


@register("openfda")
class OpenFDAConnector(Connector):
    """Pull device registration listings and drug NDC records from openFDA.

    Attributes
    ----------
    source_id:
        Matches ``sources.source_id`` = ``'openfda'``.
    raw_table:
        Destination table ``raw_regulatory``.
    """

    source_id: str = "openfda"
    raw_table: str = "raw_regulatory"

    def __init__(self, source_row: dict[str, Any], credential: str | None) -> None:
        super().__init__(source_row, credential)
        # API key is optional; its presence determines the polite throttle interval.
        # 240 req/min keyed  ≈ 1 req/0.25 s  →  0.26 s gap
        # 40  req/min keyless ≈ 1 req/1.5 s  →  1.50 s gap
        self._api_key: str | None = credential or None
        self.min_interval: float = 0.26 if self._api_key else 1.5

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build query params, injecting the optional API key when present."""
        p: dict[str, Any] = {}
        if self._api_key:
            p["api_key"] = self._api_key
        if extra:
            p.update(extra)
        return p

    # ------------------------------------------------------------------ #
    # probe
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """Fetch 1 record from device/registrationlisting to verify reachability + auth."""
        url = f"{_BASE_URL}/{_DEVICE_EP}"
        try:
            resp = self.http.get(url, params=self._params({"limit": 1}))
        except Exception as exc:
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail)

        if resp.status_code == 200:
            try:
                data = resp.json()
                results: list[dict[str, Any]] = data.get("results") or []
            except Exception:
                return ProbeResult(
                    "SCHEMA_MISMATCH",
                    "openFDA /device/registrationlisting.json returned non-JSON",
                )
            if not results:
                return ProbeResult("EMPTY", "openFDA reachable but returned zero records")
            return ProbeResult(
                "OK",
                f"openFDA reachable (API key={'present' if self._api_key else 'absent'})",
                results[:1],
            )

        status = classify_http_error(resp.status_code, resp.text)
        return ProbeResult(status, f"HTTP {resp.status_code}: {resp.text[:200]}")

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
        """Paginate device/registrationlisting then drug/ndc; tag each record with ``_endpoint``.

        ``taxonomy`` and ``geographies`` are informational.  openFDA does not
        expose a bulk modification-date filter for these endpoints, so the full
        current snapshot (up to the 25 k skip cap) is returned on every run.
        Idempotent composite-key upserts in the pipeline deduplicate repeats.
        """
        if since:
            logger.info(
                "openfda pull: since=%s noted — openFDA has no bulk date filter; "
                "returning full snapshot up to the 25 k skip cap per endpoint.",
                since,
            )

        for endpoint in (_DEVICE_EP, _DRUG_EP):
            logger.info("openfda: paginating %s", endpoint)
            yield from self._paginate(endpoint)

    def _paginate(self, endpoint: str) -> Iterator[dict[str, Any]]:
        """Yield all records from *endpoint*, stopping at the openFDA skip cap.

        On the first page the total-record count is read from ``meta.results.total``
        and a warning is emitted if it exceeds the cap (directing operators to use
        the AccessGUDID bulk connector for complete coverage).
        """
        url = f"{_BASE_URL}/{endpoint}"
        skip = 0
        page = 0

        while True:
            params = self._params({"limit": _PAGE_SIZE, "skip": skip})
            try:
                resp = self.http.get(url, params=params)
            except Exception as exc:
                status, detail = classify_exception(exc)
                logger.warning(
                    "openfda %s page=%d skip=%d: %s — %s",
                    endpoint, page, skip, status, detail,
                )
                return

            if resp.status_code != 200:
                hs = classify_http_error(resp.status_code, resp.text)
                logger.warning(
                    "openfda %s page=%d skip=%d: HTTP %d (%s)",
                    endpoint, page, skip, resp.status_code, hs,
                )
                return

            try:
                data = resp.json()
            except Exception:
                logger.warning(
                    "openfda %s page=%d: non-JSON body — aborting pagination.", endpoint, page
                )
                return

            results: list[dict[str, Any]] = data.get("results") or []
            if not results:
                logger.debug(
                    "openfda %s: no records at skip=%d — pagination complete.", endpoint, skip
                )
                return

            # First-page metadata logging + cap warning.
            if page == 0:
                meta_total = (
                    ((data.get("meta") or {}).get("results") or {}).get("total")
                )
                if meta_total:
                    logger.info("openfda %s: %d total records reported.", endpoint, meta_total)
                    if meta_total > _SKIP_CAP + _PAGE_SIZE:
                        logger.warning(
                            "openfda %s: %d records exceed the 25 k pagination cap. "
                            "Use fda_accessgudid bulk ZIP for complete device-master coverage.",
                            endpoint, meta_total,
                        )

            for record in results:
                # Tag each record with its source endpoint so normalize() can dispatch.
                record["_endpoint"] = endpoint
                yield record

            skip += len(results)
            page += 1

            if len(results) < _PAGE_SIZE:
                # Partial page — this was the last one.
                logger.debug("openfda %s: partial page at skip=%d; done.", endpoint, skip)
                break
            if skip >= _SKIP_CAP:
                logger.info(
                    "openfda %s: reached 25 k skip cap after %d records; stopping.",
                    endpoint, skip,
                )
                break

    # ------------------------------------------------------------------ #
    # normalize
    # ------------------------------------------------------------------ #

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map a verbatim openFDA record to ``raw_regulatory`` typed columns.

        Dispatches on the ``_endpoint`` key injected during :meth:`_paginate`.
        Returns ``{}`` for unrecognised shapes (pipeline skips empty mappings).
        """
        endpoint = raw.get("_endpoint", "")
        if "registrationlisting" in endpoint:
            return self._norm_device(raw)
        if "drug/ndc" in endpoint:
            return self._norm_drug(raw)
        logger.warning("openfda normalize: unrecognised _endpoint %r — skipping row.", endpoint)
        return {}

    @staticmethod
    def _norm_device(raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize a ``device/registrationlisting`` record to ``raw_regulatory`` columns.

        Field mapping:
            registration_id  <- registration.registration_number
            holder           <- registration.owner_operator.firm_name
            product_code     <- products[0].product_code
            country          <- registration.addresses[0].country_code  (default "US")
            status           <- registration.status
        """
        reg: dict[str, Any] = raw.get("registration") or {}
        owner_op: dict[str, Any] = reg.get("owner_operator") or {}
        addresses: list[Any] = reg.get("addresses") or []
        products: list[Any] = raw.get("products") or []

        # FDA domestic listings may omit country_code; default to "US".
        country = "US"
        if addresses and isinstance(addresses[0], dict):
            country = addresses[0].get("country_code") or "US"

        product_code: str | None = None
        if products and isinstance(products[0], dict):
            product_code = products[0].get("product_code")

        return {
            "registration_id": reg.get("registration_number"),
            "holder": owner_op.get("firm_name") or owner_op.get("company_name"),
            "product_code": product_code,
            "country": country,
            "status": reg.get("status"),
        }

    @staticmethod
    def _norm_drug(raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize a ``drug/ndc`` record to ``raw_regulatory`` columns.

        Field mapping:
            registration_id  <- product_ndc
            holder           <- labeler_name  (→ brand_name → generic_name)
            product_code     <- application_number  (→ product_type)
            country          <- "US"  (FDA jurisdiction)
            status           <- marketing_status
        """
        return {
            "registration_id": raw.get("product_ndc"),
            "holder": (
                raw.get("labeler_name")
                or raw.get("brand_name")
                or raw.get("generic_name")
            ),
            # NDA/ANDA/BLA application number when present; else HUMAN OTC DRUG etc.
            "product_code": raw.get("application_number") or raw.get("product_type"),
            "country": "US",
            "status": raw.get("marketing_status"),
        }
