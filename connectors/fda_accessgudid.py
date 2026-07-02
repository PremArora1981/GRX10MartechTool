"""FDA AccessGUDID connector — weekly device-record delta ZIP.

Lands data into ``raw_regulatory`` with typed columns:
    registration_id / holder / product_code / country / status

Source details (connector-catalog §2):
    source_id   : fda_accessgudid
    raw_table   : raw_regulatory
    class       : A
    auth        : none  (public bulk download, no API key required)
    base_url    : https://accessgudid.nlm.nih.gov
    refresh     : weekly delta ZIP (~1 MB, pipe-delimited UTF-8)
                  full release ~511 MB / 5.1 M records — delta only on cron

Strategy:
    1. probe()  — GET /download page; verify at least one delta ZIP link exists.
    2. pull()   — discover the latest delta href from page HTML, download the
                  ZIP in-memory (~1 MB safe to buffer), extract device.txt
                  (pipe-delimited), yield each row verbatim as a dict.
    3. normalize() — map typed raw_regulatory columns.

Column-name priority lists (_COL_*) handle minor field-name variations across
GUDID schema versions.  All AccessGUDID records are FDA-regulated; ``country``
is always ``"US"``.

The ``_source_href`` key injected into each yielded row is stored verbatim in
``raw_json`` for drillability; it is ignored by normalize().
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from html.parser import HTMLParser
from typing import Any, Iterator

from connectors.base import (
    Connector,
    ProbeResult,
    classify_exception,
    classify_http_error,
)
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.fda_accessgudid")

_BASE_URL = "https://accessgudid.nlm.nih.gov"
_DOWNLOAD_PATH = "/download"

# Column-name priority lists — first non-empty value in the row wins.
# Names vary slightly across GUDID schema release versions.
_COL_REG_ID: tuple[str, ...] = ("primaryDI", "primary_di", "di", "udi")
_COL_HOLDER: tuple[str, ...] = (
    "companyName", "company_name", "labelerName", "labeler_name"
)
_COL_PRODUCT: tuple[str, ...] = (
    "productCode", "product_code", "classificationName", "classification_name",
    "gmdnPTName", "gmdn_pt_name",
)
_COL_STATUS: tuple[str, ...] = (
    "publicVersionStatus", "public_version_status",
    "commercialDistributionStatus", "commercial_distribution_status",
)
_COUNTRY = "US"  # AccessGUDID is wholly under FDA (US) regulatory jurisdiction.


# ------------------------------------------------------------------ #
# HTML link scraping (stdlib only — no BeautifulSoup dependency)
# ------------------------------------------------------------------ #

class _HrefParser(HTMLParser):
    """Minimal HTMLParser subclass that collects all href attribute values."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for name, val in attrs:
                if name == "href" and val:
                    self.hrefs.append(val)


def _find_latest_delta_href(html: str) -> str | None:
    """Return the most-recent delta ZIP href from the AccessGUDID download page.

    Delta links are identified by containing "DeltaFile" (case-insensitive) in
    the href and ending with ".zip".  When multiple files are present the one
    with the latest embedded YYYYMMDD date is returned.

    Returns ``None`` if no matching link is found (page structure changed).
    """
    parser = _HrefParser()
    parser.feed(html)
    candidates = [
        h for h in parser.hrefs
        if re.search(r"deltafile", h, re.IGNORECASE) and h.lower().endswith(".zip")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda h: _date_from_href(h) or "00000000", reverse=True)
    return candidates[0]


def _date_from_href(href: str) -> str | None:
    """Extract the YYYYMMDD date embedded in a delta-file href (or ``None``)."""
    m = re.search(r"(\d{8})", href)
    return m.group(1) if m else None


def _first_of(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty string value from *row* for the given key priority list."""
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


# ------------------------------------------------------------------ #
# Connector
# ------------------------------------------------------------------ #

@register("fda_accessgudid")
class FDAAccessGUDIDConnector(Connector):
    """FDA AccessGUDID weekly delta ZIP connector.

    Downloads the most-recent weekly delta from
    https://accessgudid.nlm.nih.gov/download, extracts the pipe-delimited
    ``device.txt`` file, and yields each row verbatim into ``raw_regulatory``.

    No API key or authentication is required; the delta ZIP is a public download.

    Attributes
    ----------
    source_id:
        Matches ``sources.source_id`` = ``'fda_accessgudid'``.
    raw_table:
        Destination table ``raw_regulatory``.
    min_interval:
        Conservative throttle — the download is one large request; 0.5 s gap
        between page probe and ZIP download is courteous.
    """

    source_id: str = "fda_accessgudid"
    raw_table: str = "raw_regulatory"
    min_interval: float = 0.5

    def __init__(self, source_row: dict[str, Any], credential: str | None) -> None:
        super().__init__(source_row, credential)
        if credential:
            logger.debug(
                "fda_accessgudid: no credential is required for this source; "
                "supplied value will not be used."
            )

    # ------------------------------------------------------------------ #
    # probe
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """GET the download page and confirm at least one delta ZIP link is listed.

        Returns
        -------
        OK
            Download page is reachable and at least one delta ZIP link was found.
        SCHEMA_MISMATCH
            Page returned 200 but no delta ZIP links were found — the page
            structure has likely changed and the connector needs updating.
        UNREACHABLE / AUTH_FAILED / RATE_LIMITED / QUOTA_EXHAUSTED
            HTTP or transport error mapped through the 7-state taxonomy.
        """
        url = f"{_BASE_URL}{_DOWNLOAD_PATH}"
        try:
            resp = self.http.get(url)
        except Exception as exc:
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail)

        if resp.status_code != 200:
            status = classify_http_error(resp.status_code, resp.text)
            return ProbeResult(status, f"HTTP {resp.status_code}: {resp.text[:200]}")

        href = _find_latest_delta_href(resp.text)
        if not href:
            return ProbeResult(
                "SCHEMA_MISMATCH",
                "AccessGUDID download page reachable (HTTP 200) but no delta ZIP "
                "links found — page structure may have changed.",
            )

        return ProbeResult(
            "OK",
            f"AccessGUDID download page reachable; latest delta: {href}",
            {"latest_delta_href": href},
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
        """Download the latest weekly delta ZIP and yield each device row.

        Lookup / filter behaviour:
        * ``taxonomy`` and ``geographies`` are informational; AccessGUDID does
          not support server-side filtering on these dimensions.
        * ``since``: if the embedded date in the discovered delta filename is
          older than ``since``, no rows are yielded — the pipeline already
          ingested that file on a previous run (idempotent behaviour).
        * When ``since`` is ``None`` (first-ever run), the full delta is pulled.

        Yields
        ------
        dict
            One verbatim row per device record from ``device.txt``.  Each row
            includes a ``_source_href`` key (the download URL) for drillability.
        """
        href = self._discover_delta_href()
        if not href:
            return  # error already logged in _discover_delta_href

        # Respect the lookback window: skip if the delta predates ``since``.
        if since:
            file_date = _date_from_href(href)
            since_compact = since[:10].replace("-", "")  # YYYYMMDD
            if file_date and file_date < since_compact:
                logger.info(
                    "fda_accessgudid: delta file date %s < since %s — "
                    "nothing new to ingest; skipping download.",
                    file_date,
                    since_compact,
                )
                return

        download_url = href if href.startswith("http") else f"{_BASE_URL}{href}"
        logger.info("fda_accessgudid: downloading delta ZIP from %s", download_url)

        zip_bytes = self._download_zip(download_url)
        if zip_bytes is None:
            return  # error already logged in _download_zip

        yield from self._parse_zip(zip_bytes, source_href=download_url)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _discover_delta_href(self) -> str | None:
        """Scrape /download and return the most-recent delta ZIP href (or ``None``)."""
        url = f"{_BASE_URL}{_DOWNLOAD_PATH}"
        try:
            resp = self.http.get(url)
        except Exception as exc:
            status, detail = classify_exception(exc)
            logger.error(
                "fda_accessgudid: download page unreachable (%s): %s", status, detail
            )
            return None

        if resp.status_code != 200:
            hs = classify_http_error(resp.status_code, resp.text)
            logger.error(
                "fda_accessgudid: download page returned HTTP %d (%s)",
                resp.status_code, hs,
            )
            return None

        href = _find_latest_delta_href(resp.text)
        if not href:
            logger.error(
                "fda_accessgudid: no delta ZIP links found on %s; "
                "page structure may have changed — connector needs review.",
                url,
            )
        return href

    def _download_zip(self, url: str) -> bytes | None:
        """Download the delta ZIP and return its raw bytes, or ``None`` on error.

        The weekly delta is ~1 MB; loading it fully into memory is safe.  If the
        source ever shifts to the full release (~511 MB) this method should be
        updated to stream into a :func:`tempfile.NamedTemporaryFile`.
        """
        try:
            resp = self.http.get(url)
        except Exception as exc:
            status, detail = classify_exception(exc)
            logger.error("fda_accessgudid: ZIP download failed (%s): %s", status, detail)
            return None

        if resp.status_code != 200:
            hs = classify_http_error(resp.status_code, resp.text)
            logger.error(
                "fda_accessgudid: ZIP download returned HTTP %d (%s)",
                resp.status_code, hs,
            )
            return None

        return resp.content

    def _parse_zip(
        self, zip_bytes: bytes, *, source_href: str = ""
    ) -> Iterator[dict[str, Any]]:
        """Extract ``device.txt`` from the ZIP archive and yield each row as a dict.

        File selection:
            Primary  — any entry whose base name matches ``\\bdevice\\b`` and ends
                       in ``.txt`` (case-insensitive).
            Fallback — first ``.txt`` file in the archive.

        Yields
        ------
        dict
            One entry per CSV row, augmented with ``_source_href`` for auditability.
            All values are strings (as delivered by csv.DictReader with a ``|``
            delimiter).
        """
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as exc:
            logger.error(
                "fda_accessgudid: downloaded content from %s is not a valid ZIP: %s",
                source_href, exc,
            )
            return

        names = zf.namelist()
        logger.debug("fda_accessgudid: ZIP contains: %s", names)

        # Prefer the canonical device records file.
        device_entry: str | None = next(
            (
                n for n in names
                if re.search(r"\bdevice\b", n, re.IGNORECASE) and n.lower().endswith(".txt")
            ),
            next((n for n in names if n.lower().endswith(".txt")), None),
        )

        if not device_entry:
            logger.error(
                "fda_accessgudid: no .txt file found in ZIP (contents: %s); "
                "cannot parse device records.",
                names,
            )
            return

        logger.info("fda_accessgudid: parsing '%s' from ZIP", device_entry)
        row_count = 0
        with zf.open(device_entry) as raw_bytes:
            text = io.TextIOWrapper(raw_bytes, encoding="utf-8", errors="replace")
            reader = csv.DictReader(text, delimiter="|")
            for row in reader:
                row_dict = dict(row)
                row_dict["_source_href"] = source_href
                yield row_dict
                row_count += 1

        logger.info(
            "fda_accessgudid: yielded %d rows from '%s'", row_count, device_entry
        )

    # ------------------------------------------------------------------ #
    # normalize
    # ------------------------------------------------------------------ #

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one AccessGUDID device row to ``raw_regulatory`` typed columns.

        Uses column-name priority lists to handle minor field-name variations
        across GUDID schema release versions.  ``country`` is always ``"US"``
        since all AccessGUDID records are under FDA regulatory jurisdiction.

        Returns
        -------
        dict
            Keys: registration_id, holder, product_code, country, status.
            Any value may be ``None`` if the field is absent in this row.
        """
        return {
            "registration_id": _first_of(raw, _COL_REG_ID),
            "holder": _first_of(raw, _COL_HOLDER),
            "product_code": _first_of(raw, _COL_PRODUCT),
            "country": _COUNTRY,
            "status": _first_of(raw, _COL_STATUS),
        }
