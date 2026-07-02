"""USPTO PatentsView connector — lands data into ``raw_patents``.

Source
------
Endpoint : POST https://search.patentsview.org/api/v1/patent/
Auth     : ``X-Api-Key`` request header (free API key from patentsview.org).
Class    : C — triangulation / patent-activity proxy only (connector catalog).
Rate     : ~45 requests/min (catalog note) → :attr:`min_interval` = 1.4 s.

IMPORTANT
---------
The legacy host ``api.patentsview.org`` was **sunset in May 2025**.
This connector targets the replacement host ``search.patentsview.org`` only.
The old v0.3 query DSL (JSON ``?q=`` query parameter) is gone; we now POST
a structured query body.

Design notes
------------
* :meth:`pull` derives CPC group prefixes from taxonomy ``hs_codes`` via a
  curated mapping table.  When no hs_codes are present it falls back to all
  electronics patents (CPC section H).
* Pagination uses the cursor in ``_meta.next``.  The cursor is passed back via
  ``o._after`` in the next request, so large result sets are handled without
  offset arithmetic.
* Each yielded dict is **one verbatim patent object** from the API (the element
  inside ``patents[]``), NOT a full page.  This makes ``normalize()`` a simple
  1-to-1 mapping.
* Missing or None ``app_date`` / ``filing_date`` is preserved as None rather
  than coerced to a sentinel, because the pipeline treats None correctly.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from connectors.base import Connector, ProbeResult, classify_exception, classify_http_error
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.uspto_patentsview")

# ──────────────────────────────────────────────────────────────────────────────
# CPC group prefix → HS-code stem mapping (in reverse: hs stem → CPC prefixes)
#
# Source: USPTO CPC schedule + IPC Concordance Table + connector catalog.
# The taxonomy hs_codes are 4-digit or 6-digit HS codes (e.g. "8532", "8504.40").
# We match on the first 4 digits.
# ──────────────────────────────────────────────────────────────────────────────
_HS4_TO_CPC: dict[str, list[str]] = {
    "8532": ["H01G"],            # capacitors, varistors → H01G
    "8533": ["H01C"],            # resistors → H01C
    "8504": ["H01F", "H01G"],    # transformers, inductors → H01F; also H01G (capacitive PSUs)
    "8541": ["H01L"],            # diodes, transistors → H01L
    "8542": ["H01L"],            # integrated circuits → H01L
    "8536": ["H01R", "H01H"],    # connectors, switches → H01R, H01H
    "8538": ["H01H"],            # parts for switchgear → H01H
    "8539": ["H05B"],            # lamps → H05B (lighting)
    "8540": ["H01J"],            # valves, cathode-ray → H01J
    "8543": ["H05K", "H01L"],    # other electrical machines → H05K / H01L
    "8544": ["H01B"],            # insulated wire/cable → H01B
    "8545": ["H01B"],            # carbon electrodes → H01B
    "8546": ["H01T"],            # electrical insulators → H01T
    "8547": ["H01B"],            # insulating fittings → H01B
    "8548": ["H01L", "H05K"],    # other electrical parts → H01L, H05K
}

# Fields requested from the PatentsView API.  Requesting nested entities as
# dot-notation fields is the documented way to include sub-entities.
_PATENT_FIELDS: list[str] = [
    "patent_id",
    "app_date",
    "patent_date",
    "patent_title",
    "assignees.assignee_organization",
    "assignees.assignee_first_name",
    "assignees.assignee_last_name",
    "assignees.assignee_country",
    "cpcs.cpc_section_id",
    "cpcs.cpc_subsection_id",
    "cpcs.cpc_group_id",
    "cpcs.cpc_subgroup_id",
]

_DEFAULT_PAGE_SIZE = 100          # max per page (API limit varies; 100 is safe)
_FALLBACK_CPC_SECTION = "H"      # all electronics if no taxonomy mapping found
_DEFAULT_SINCE = "2019-01-01"    # 5-year lookback when caller passes since=None


def _cpc_prefixes_from_taxonomy(taxonomy: list[dict[str, Any]]) -> list[str]:
    """Return a de-duplicated list of CPC group prefixes for the given taxonomy rows.

    Each taxonomy row carries an ``hs_codes`` list (4- or 6-digit HS codes).
    We look up the CPC groups in ``_HS4_TO_CPC`` on the first 4 digits and
    collect all matches.  Returns ``[_FALLBACK_CPC_SECTION]`` when nothing matches.
    """
    seen: set[str] = set()
    result: list[str] = []
    for row in taxonomy:
        for hs in row.get("hs_codes") or []:
            stem = str(hs).replace(".", "")[:4]
            for cpc in _HS4_TO_CPC.get(stem, []):
                if cpc not in seen:
                    seen.add(cpc)
                    result.append(cpc)
    return result if result else [_FALLBACK_CPC_SECTION]


def _build_query(
    cpc_prefixes: list[str],
    since: str,
) -> dict[str, Any]:
    """Build the PatentsView POST body for a batch of CPC prefixes + date filter.

    Uses ``_gte`` on ``app_date`` for the date gate and ``_begins`` on
    ``cpcs.cpc_subgroup_id`` for each CPC prefix.  When only the fallback
    section (H) is present we filter by the broader ``cpcs.cpc_section_id``
    field to avoid an excessively wide ``_begins`` on the subgroup field.
    """
    date_clause: dict[str, Any] = {"_gte": {"app_date": since}}

    if len(cpc_prefixes) == 1 and len(cpc_prefixes[0]) == 1:
        # Fallback: single CPC section letter — use section field
        cpc_clause: dict[str, Any] = {"_eq": {"cpcs.cpc_section_id": cpc_prefixes[0]}}
    elif len(cpc_prefixes) == 1:
        cpc_clause = {"_begins": {"cpcs.cpc_subgroup_id": cpc_prefixes[0]}}
    else:
        cpc_clause = {
            "_or": [{"_begins": {"cpcs.cpc_subgroup_id": p}} for p in cpc_prefixes]
        }

    return {
        "q": {"_and": [date_clause, cpc_clause]},
        "f": _PATENT_FIELDS,
        "o": {"size": _DEFAULT_PAGE_SIZE},
        "s": [{"patent_id": "asc"}],
    }


@register("uspto_patentsview")
class USPTOPatentsViewConnector(Connector):
    """Connector for the USPTO PatentsView API v1 (search.patentsview.org).

    Expected ``sources`` row values
    --------------------------------
    source_id : ``uspto_patentsview``
    url_pattern : ``https://search.patentsview.org/api/v1``
    auth : ``api_key``
    auth_secret_ref : pointer to the decrypted API key (header: ``X-Api-Key``)
    raw_table : ``raw_patents``
    class : ``C``
    """

    source_id: str = "uspto_patentsview"
    raw_table: str = "raw_patents"

    # ~45 requests/min (catalog); leave headroom → 1.4 s between requests.
    min_interval: float = 1.4

    # ── private helpers ─────────────────────────────────────────────────────

    def _base(self) -> str:
        """Resolved base URL (strips trailing slash)."""
        return (self.base_url or "https://search.patentsview.org/api/v1").rstrip("/")

    def _headers(self) -> dict[str, str]:
        """Build request headers, embedding the API key."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.credential:
            headers["X-Api-Key"] = self.credential
        return headers

    # ── Connector contract ───────────────────────────────────────────────────

    def default_headers(self) -> dict[str, str]:
        return self._headers()

    def probe(self) -> ProbeResult:
        """POST a minimal single-result query to verify auth and reachability."""
        if self.missing_credential():
            return self.auth_failed_probe()

        body = {
            "q": {"_gte": {"patent_date": "2024-01-01"}},
            "f": ["patent_id", "patent_date"],
            "o": {"size": 1},
            "s": [{"patent_id": "asc"}],
        }
        try:
            resp = self.http.post(
                f"{self._base()}/patent/",
                json=body,
                headers=self._headers(),
            )
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail)

        if resp.status_code == 200:
            try:
                data = resp.json()
                patents = data.get("patents") or []
                if not patents:
                    return ProbeResult("EMPTY", "authenticated but 0 patents returned", None)
                return ProbeResult("OK", f"HTTP 200; {len(patents)} patent(s) in probe", patents[:1])
            except Exception:  # noqa: BLE001
                return ProbeResult(
                    "SCHEMA_MISMATCH",
                    f"200 OK but JSON parse failed: {resp.text[:200]}",
                )

        status = classify_http_error(resp.status_code, resp.text)
        return ProbeResult(status, f"HTTP {resp.status_code}: {resp.text[:300]}")

    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield one verbatim patent dict per patent for the given taxonomy scope.

        ``taxonomy`` rows drive the CPC filter via ``hs_codes``.
        ``since`` is treated as the filing date lower bound (``app_date``).
        ``geographies`` is accepted but not used for API-side filtering — the
        PatentsView API does not support assignee-country filters efficiently;
        the country column is populated in :meth:`normalize` and can be filtered
        downstream.
        """
        if self.missing_credential():
            logger.warning("%s: no credential — yielding nothing", self.source_id)
            return

        since_date = since[:10] if since else _DEFAULT_SINCE
        cpc_prefixes = _cpc_prefixes_from_taxonomy(taxonomy)
        logger.info(
            "%s: pulling patents; CPC prefixes=%s since=%s",
            self.source_id, cpc_prefixes, since_date,
        )

        base_body = _build_query(cpc_prefixes, since_date)
        cursor: str | None = None
        page_num = 0

        while True:
            body = dict(base_body)
            if cursor is not None:
                body["o"] = {**body.get("o", {}), "_after": cursor}

            try:
                resp = self.http.post(
                    f"{self._base()}/patent/",
                    json=body,
                    headers=self._headers(),
                )
            except Exception as exc:  # noqa: BLE001
                status, detail = classify_exception(exc)
                logger.error("%s: transport error on page %d — %s: %s",
                             self.source_id, page_num, status, detail)
                return

            if resp.status_code != 200:
                status = classify_http_error(resp.status_code, resp.text)
                logger.warning(
                    "%s: HTTP %d on page %d — %s: %s",
                    self.source_id, resp.status_code, page_num, status, resp.text[:300],
                )
                return

            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                logger.error("%s: JSON decode failed on page %d", self.source_id, page_num)
                return

            patents: list[dict[str, Any]] = data.get("patents") or []
            meta: dict[str, Any] = data.get("_meta") or {}

            if not patents:
                logger.info("%s: empty page %d — done", self.source_id, page_num)
                return

            for patent in patents:
                yield patent

            page_num += 1
            cursor = meta.get("next") or None
            if cursor is None:
                logger.info(
                    "%s: cursor exhausted after %d pages; total=%s",
                    self.source_id, page_num, meta.get("total_patent_count", "?"),
                )
                return

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one verbatim patent object to ``raw_patents`` typed columns.

        Columns produced
        ----------------
        patent_id  : patent number string.
        assignee   : first assignee organisation name (or "Last, First" for individuals).
        cpc        : most-specific CPC code of the first classification entry.
        filing_date: ``app_date`` string (ISO date) — the pipeline casts to DATE.
        country    : ISO-2 country from the first assignee; None when absent.
        """
        # ── patent identity ───────────────────────────────────────────────
        patent_id: str | None = raw.get("patent_id")

        # ── assignee ──────────────────────────────────────────────────────
        assignees: list[dict[str, Any]] = raw.get("assignees") or []
        assignee: str | None = None
        country: str | None = None
        if assignees:
            first = assignees[0]
            org = first.get("assignee_organization")
            if org:
                assignee = str(org).strip() or None
            else:
                first_n = first.get("assignee_first_name") or ""
                last_n = first.get("assignee_last_name") or ""
                combined = ", ".join(filter(None, [last_n, first_n]))
                assignee = combined or None
            raw_country = first.get("assignee_country")
            country = str(raw_country).strip().upper() if raw_country else None

        # ── CPC classification ────────────────────────────────────────────
        cpcs: list[dict[str, Any]] = raw.get("cpcs") or []
        cpc: str | None = None
        if cpcs:
            first_cpc = cpcs[0]
            # Prefer the most specific available level: subgroup > group > subsection.
            cpc = (
                first_cpc.get("cpc_subgroup_id")
                or first_cpc.get("cpc_group_id")
                or first_cpc.get("cpc_subsection_id")
                or first_cpc.get("cpc_section_id")
            )
            if cpc:
                cpc = str(cpc).strip() or None

        # ── dates ─────────────────────────────────────────────────────────
        filing_date: str | None = raw.get("app_date")  # ISO date string; DB casts to DATE

        return {
            "patent_id": patent_id,
            "assignee": assignee,
            "cpc": cpc,
            "filing_date": filing_date,
            "country": country,
        }
