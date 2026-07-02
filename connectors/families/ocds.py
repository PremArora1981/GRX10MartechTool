"""Generic OCDS (Open Contracting Data Standard) family connector.

One connector class serves 100+ government procurement publishers.  Per-
publisher endpoint configuration is loaded from
``connectors/ocds_publishers.yaml``; each publisher maps to its own
``sources`` row (``source_id = ocds_<key>``) and is instantiated by the
same class at pipeline time.

Architecture
------------
``OcdsConnector`` subclasses :class:`~connectors.families.rest_family.RestFamilyConnector`
and uses its pagination and HTTP machinery.  ``pull()`` is overridden to
*explode* OCDS releases into per-award records before yielding, because
one OCDS release can carry multiple awards (and multiple suppliers per
award).  This gives the ``raw_procurement`` table one row per
publisher-award, which is the correct granularity for the
``tender_award_aggregation`` estimation method.

OCDS Release structure (abbreviated)::

    release-package -> releases[]
        ocid: str                         # globally-unique contract ID
        buyer: {name: str, ...}
        awards[]:
            id: str
            title: str
            value: {amount: float, currency: str}
            suppliers[]: [{name: str, ...}]
            contractPeriod: {startDate: str, endDate: str}
        tender:
            title: str
            procurementMethod: str
            items[]: [{description: str, ...}]

Spec ref: https://standard.open-contracting.org

Invariants
----------
* No fabrication: if a key is absent the normalized column is ``None``.
* One source row per publisher; the YAML is seeded on startup via the
  config loader (``pipeline/run.py``); the DB is authoritative after that.
* The raw ``raw_json`` stores the per-award exploded record (still fully
  verbatim from the source), not just the high-level release.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterator

import yaml  # PyYAML is a declared dependency of the project

from connectors.base import ProbeResult
from connectors.families.rest_family import EndpointSpec, RestFamilyConnector
from connectors.registry import register

logger = logging.getLogger("grx10.connectors.ocds")

# Path is relative to this file's package, not the CWD.
_YAML_PATH = Path(__file__).resolve().parent.parent / "ocds_publishers.yaml"

# Cache: loaded once per process, keyed by source_id.
_PUBLISHER_CACHE: dict[str, dict[str, Any]] | None = None


def _load_publishers() -> dict[str, dict[str, Any]]:
    """Load and cache the OCDS publisher definitions from YAML.

    Returns a mapping of ``source_id -> publisher_config_dict``.
    Raises ``FileNotFoundError`` if the YAML is missing; ``yaml.YAMLError``
    if the YAML is malformed.
    """
    global _PUBLISHER_CACHE
    if _PUBLISHER_CACHE is not None:
        return _PUBLISHER_CACHE

    with open(_YAML_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    pubs: dict[str, dict[str, Any]] = {}
    for entry in raw.get("publishers") or []:
        sid = entry.get("source_id")
        if sid:
            pubs[sid] = entry

    _PUBLISHER_CACHE = pubs
    logger.info("loaded %d OCDS publisher(s) from %s", len(pubs), _YAML_PATH)
    return pubs


def _pub_to_endpoint(pub: dict[str, Any]) -> EndpointSpec:
    """Build an :class:`EndpointSpec` from a publisher config dict.

    ``endpoint`` is stored as a relative path; the ``HttpClient`` inside
    ``OcdsConnector`` is created with ``base_url = source_row["url_pattern"]``
    (from :meth:`Connector.__init__`), so httpx joins path onto that base.
    This matches the pattern used by every other connector in the framework.
    """
    extra: dict[str, Any] = pub.get("extra_params") or {}
    return EndpointSpec(
        name=pub.get("name", pub.get("source_id", "ocds")),
        path=pub.get("endpoint", ""),   # relative path; base_url from source_row
        method=(pub.get("method") or "GET").upper(),
        params=dict(extra),
        records_path=pub.get("records_path"),
        page_param=pub.get("page_param"),
        page_size_param=pub.get("page_size_param"),
        page_size=int(pub.get("page_size") or 100),
        start_page=1,
        max_pages=int(pub.get("max_pages") or 20),
        record_tag={"_country": pub.get("country", "")},
    )


# --------------------------------------------------------------------------- #
# OCDS field-extraction helpers
# --------------------------------------------------------------------------- #

def _safe_str(value: Any) -> str | None:
    """Return ``str(value)`` or ``None`` when value is empty/None."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _extract_buyer(release: dict[str, Any]) -> str | None:
    """Extract buyer name from a release, tolerating absent / nested keys."""
    buyer = release.get("buyer")
    if isinstance(buyer, dict):
        return _safe_str(buyer.get("name"))
    return None


def _extract_period(award: dict[str, Any]) -> str | None:
    """Return 'YYYY-MM/YYYY-MM' (or 'YYYY-MM') from contractPeriod."""
    cp = award.get("contractPeriod") or {}
    start = _safe_str(cp.get("startDate") or "")
    end = _safe_str(cp.get("endDate") or "")
    # Normalise ISO timestamps to 'YYYY-MM'.
    start = (start or "")[:7]
    end = (end or "")[:7]
    if start and end and end >= start:
        return f"{start}/{end}"
    return start or None


def _award_value_usd(award: dict[str, Any]) -> float | None:
    """Return award value in USD (or raw currency amount if not USD).

    Currency conversion is out of scope for this connector — we store the
    raw numeric amount regardless of currency.  A downstream enrichment step
    can apply FX rates from ``raw_external_metrics``.
    """
    val = award.get("value") or {}
    amount = val.get("amount")
    if amount is None:
        return None
    try:
        return float(amount)
    except (TypeError, ValueError):
        return None


def _first_supplier(award: dict[str, Any]) -> str | None:
    """Return the name of the first supplier in an award (if any)."""
    suppliers = award.get("suppliers")
    if isinstance(suppliers, list) and suppliers:
        first = suppliers[0]
        if isinstance(first, dict):
            return _safe_str(first.get("name"))
    return None


_WHITESPACE = re.compile(r"\s+")


def _compact(text: str | None) -> str | None:
    """Collapse runs of whitespace in a string (cosmetic normalisation)."""
    if not text:
        return None
    return _WHITESPACE.sub(" ", text).strip() or None


# --------------------------------------------------------------------------- #
# OCDS connector
# --------------------------------------------------------------------------- #

@register("ocds")
class OcdsConnector(RestFamilyConnector):
    """Generic OCDS procurement connector driven by ``ocds_publishers.yaml``.

    One instance per publisher; the publisher is identified by the
    ``source_id`` on the source row (e.g. ``ocds_paraguay``).

    ``probe()`` and the pagination loop are inherited from
    :class:`~connectors.families.rest_family.RestFamilyConnector`.  ``pull()``
    is overridden to explode each OCDS release into individual per-award
    records before yielding; ``normalize()`` maps those exploded dicts to
    typed ``raw_procurement`` columns.
    """

    source_id = "ocds"
    raw_table = "raw_procurement"
    # Most OCDS publishers are small national portals — be polite.
    min_interval = 0.5

    # Class-level field map (used by the inherited normalize() if pull() is
    # not overriding).  Overridden in __init__ for exploded records.
    field_map: dict[str, str] = {}

    def __init__(self, source_row: Any, credential: str | None) -> None:  # noqa: ANN001
        """Resolve the publisher config for this source_id and build endpoints."""
        super().__init__(source_row, credential)

        # Always initialize _pub_country so normalize() is always safe to call.
        self._pub_country: str = ""

        sid = self.source_id  # set by Connector.__init__ from source_row

        try:
            publishers = _load_publishers()
        except FileNotFoundError:
            logger.error(
                "ocds_publishers.yaml not found at %s; %s will return SCHEMA_MISMATCH",
                _YAML_PATH, sid,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to load ocds_publishers.yaml: %s", exc)
            return

        pub = publishers.get(sid)
        if pub is None:
            # May still work if source_row["config"] carries endpoints (generic-REST path).
            if not self.endpoints:
                logger.warning(
                    "%s: not found in ocds_publishers.yaml and no config on source row",
                    sid,
                )
            return

        # Publisher config from YAML takes precedence over class attributes;
        # source_row["config"] (from the DB) takes the highest precedence (handled
        # in RestFamilyConnector.__init__ already).  Only inject YAML config when
        # no DB config was provided.
        if not self.endpoints:
            self.endpoints = [_pub_to_endpoint(pub)]

        # Override country from YAML (may also arrive via record_tag in the endpoint).
        self._pub_country = pub.get("country", "")

    # ------------------------------------------------------------------ #
    # Pull — override to explode releases into per-award records
    # ------------------------------------------------------------------ #

    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield one exploded dict per OCDS award (not one per release).

        Each yielded dict is stored verbatim as ``raw_procurement.raw_json``.
        The top-level keys are the full OCDS release fields, with
        ``_award`` containing the specific award object and ``_country``
        injected from the publisher's YAML configuration.

        Releases that carry no ``awards`` array (e.g. tender-stage-only
        releases) are skipped — they do not belong in ``raw_procurement``
        until an award is made.
        """
        for release in super().pull(
            taxonomy=taxonomy, geographies=geographies, since=since
        ):
            yield from self._explode_release(release)

    def _explode_release(
        self, release: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        """Expand one OCDS release into per-award verbatim records.

        Each yielded dict contains:
        * all top-level release fields (ocid, buyer, tender, …)
        * ``_award`` — the specific award sub-object
        * ``_country`` — ISO-2 code from the publisher config (or record_tag)
        """
        awards: list[Any] = release.get("awards")
        if not isinstance(awards, list) or not awards:
            return  # no awards yet on this release — skip

        # _country may be injected by record_tag in the EndpointSpec.
        country = release.get("_country") or self._pub_country or ""

        for award in awards:
            if not isinstance(award, dict):
                continue
            # Shallow copy of the release, replacing awards with just this one.
            row: dict[str, Any] = {
                k: v for k, v in release.items()
                if k not in ("awards", "_endpoint")
            }
            row["_award"] = award
            row["_country"] = country
            yield row

    # ------------------------------------------------------------------ #
    # probe — inherited, but we override to validate OCDS shape
    # ------------------------------------------------------------------ #

    def probe(self) -> ProbeResult:
        """Probe the publisher endpoint and confirm OCDS release shape."""
        if not self.endpoints:
            return ProbeResult(
                "SCHEMA_MISMATCH",
                f"{self.source_id}: no endpoint configured "
                f"(not in ocds_publishers.yaml and no source_row config)",
                None,
            )
        result = super().probe()
        if not result.ok:
            return result

        # Extra validation: confirm the sample looks like an OCDS release.
        sample = result.sample
        if isinstance(sample, dict):
            has_ocid = "ocid" in sample
            has_awards_or_tender = "awards" in sample or "tender" in sample
            if not (has_ocid or has_awards_or_tender):
                return ProbeResult(
                    "SCHEMA_MISMATCH",
                    f"{self.source_id}: 200 OK but response does not look like "
                    f"OCDS releases (missing ocid/awards/tender). "
                    f"Verify records_path in ocds_publishers.yaml.",
                    sample,
                )
        return result

    # ------------------------------------------------------------------ #
    # normalize
    # ------------------------------------------------------------------ #

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one exploded OCDS award record to raw_procurement columns.

        Column mapping
        --------------
        award_id  <- ``_award.id`` (globally unique per OCDS: ocid + award id)
        buyer     <- ``buyer.name`` from the parent release
        supplier  <- first of ``_award.suppliers[].name``
        country   <- ``_country`` (ISO-2, injected from publisher config)
        value_usd <- ``_award.value.amount`` (currency not converted)
        period    <- ``_award.contractPeriod.startDate/endDate`` as YYYY-MM/YYYY-MM
        """
        award: dict[str, Any] = raw.get("_award") or {}

        # award_id: use the award's own id scoped to its ocid for uniqueness.
        ocid: str | None = _safe_str(raw.get("ocid"))
        award_own_id: str | None = _safe_str(award.get("id"))
        if ocid and award_own_id and not award_own_id.startswith(ocid):
            award_id: str | None = f"{ocid}-{award_own_id}"
        else:
            award_id = award_own_id or ocid

        buyer: str | None = _compact(_extract_buyer(raw))
        supplier: str | None = _compact(_first_supplier(award))
        country: str | None = _safe_str(raw.get("_country"))
        value_usd: float | None = _award_value_usd(award)
        period: str | None = _extract_period(award)

        return {
            "award_id": award_id,
            "buyer": buyer,
            "supplier": supplier,
            "country": country,
            "value_usd": value_usd,
            "period": period,
        }
