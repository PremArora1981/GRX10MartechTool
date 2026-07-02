"""Declarative REST connector family.

A single, configuration-driven connector that powers whole families of similar
JSON/REST sources — OCDS procurement publishers, ATS job boards
(Greenhouse/Lever/Ashby/SmartRecruiters), newswire RSS-as-JSON feeds, and the
**user-defined generic-REST connector** with AI-assisted field mapping
(v1-definition Q6/Q12).

A concrete family connector is described by two declarative pieces:

* an **endpoint list** (:class:`EndpointSpec`) — what to call, with what params,
  where the record list lives in the response, and how to paginate; and
* a **field map** — ``{destination_column: "json.path.to.value"}`` applied to
  each verbatim record to produce typed columns for the source's ``raw_table``.

Both can be supplied three ways, in priority order:

1. a runtime ``config`` dict on the seeded source row (the generic-REST /
   AI-mapping path — config lives in the DB, the single source of truth); else
2. class attributes on a subclass (a hand-written family connector); else
3. nothing, in which case :meth:`probe` reports ``SCHEMA_MISMATCH``.

The path mini-language used by the field map is a pragmatic JSONPath subset:

    "a.b.c"          nested object keys
    "a.0.b"          list index
    "items[2].name"  bracketed list index (normalised to dotted)
    "results[*].x"   wildcard over a list -> returns a list of matches

It deliberately avoids a third-party JSONPath dependency.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterator, Mapping

import httpx

from connectors.base import Connector, ProbeResult, classify_exception

logger = logging.getLogger("grx10.connectors.rest_family")


# --------------------------------------------------------------------------- #
# Path mini-language
# --------------------------------------------------------------------------- #
_MISSING = object()


def _tokenize(path: str) -> list[str]:
    """Normalise ``a[0].b`` / ``a[*].b`` into dotted tokens ``['a','0','b']``."""
    return [t for t in path.replace("[", ".").replace("]", "").split(".") if t != ""]


def resolve_path(obj: Any, path: str, default: Any = None) -> Any:
    """Resolve a dotted/bracketed/wildcard ``path`` against a decoded JSON value.

    Wildcards (``*``) over a list collect every match into a list. Missing keys,
    out-of-range indices, or type mismatches yield ``default`` (never raise).
    """
    if not path:
        return obj
    cur: Any = obj
    for i, token in enumerate(_tokenize(path)):
        if token == "*":
            if not isinstance(cur, list):
                return default
            remainder = ".".join(_tokenize(path)[i + 1:])
            if not remainder:
                return list(cur)
            out = [resolve_path(item, remainder, _MISSING) for item in cur]
            return [v for v in out if v is not _MISSING]
        if isinstance(cur, Mapping):
            cur = cur.get(token, _MISSING)
        elif isinstance(cur, list):
            try:
                cur = cur[int(token)]
            except (ValueError, IndexError):
                return default
        else:
            return default
        if cur is _MISSING:
            return default
    return cur


_CASTERS = {
    "str": lambda v: None if v is None else str(v),
    "int": lambda v: None if v in (None, "") else int(float(v)),
    "float": lambda v: None if v in (None, "") else float(v),
    "bool": lambda v: None if v is None else bool(v),
    "date": lambda v: _parse_date(v),
    "datetime": lambda v: _parse_datetime(v),
    "json": lambda v: None if v is None else json.dumps(v, default=str),
}


def _parse_date(v: Any) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_datetime(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def apply_field_map(
    record: Mapping[str, Any],
    field_map: Mapping[str, str],
    field_types: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project one verbatim record into typed columns via a field map.

    Parameters
    ----------
    record:
        A single decoded JSON record (one row of source data).
    field_map:
        ``{destination_column: "json.path"}``. A path may be ``"=literal"`` to
        inject a constant, useful for tagging the source/flow on every row.
    field_types:
        Optional ``{destination_column: caster}`` where caster is one of
        ``str|int|float|bool|date|datetime|json``. Unspecified columns are left
        as their raw JSON-decoded value.
    """
    field_types = field_types or {}
    out: dict[str, Any] = {}
    for column, path in field_map.items():
        if isinstance(path, str) and path.startswith("="):
            value: Any = path[1:]            # literal constant
        else:
            value = resolve_path(record, str(path))
        caster = field_types.get(column)
        if caster and value is not None:
            try:
                value = _CASTERS[caster](value)
            except (KeyError, ValueError, TypeError) as exc:
                logger.debug("cast %s -> %s failed for %r: %s", column, caster, value, exc)
                value = None
        out[column] = value
    return out


# --------------------------------------------------------------------------- #
# Endpoint specification
# --------------------------------------------------------------------------- #
@dataclass
class EndpointSpec:
    """One declarative REST endpoint within a family connector.

    Attributes
    ----------
    name:
        Human label (used in logs / record tagging).
    path:
        URL or path appended onto the source's ``url_pattern`` base.
    method:
        HTTP verb (``GET`` / ``POST``).
    params / headers / json_body:
        Request shaping. Values may contain ``{placeholders}`` filled from the
        pull context (see :meth:`RestFamilyConnector._render`).
    records_path:
        Path to the list of records inside the response. ``None`` means the
        response *is* the record (or a top-level list of records).
    page_param / page_size_param / page_size / start_page / max_pages:
        Optional simple offset/page pagination. ``max_pages = 1`` disables it.
    record_tag:
        Extra key/values merged onto every yielded record (e.g. company slug for
        an ATS board) so downstream normalisation has the context it needs.
    """

    name: str
    path: str = ""
    method: str = "GET"
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    records_path: str | None = None
    page_param: str | None = None
    page_size_param: str | None = None
    page_size: int = 100
    start_page: int = 1
    max_pages: int = 1
    record_tag: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EndpointSpec":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------- #
# The family connector
# --------------------------------------------------------------------------- #
class RestFamilyConnector(Connector):
    """Config-driven connector for families of REST/JSON sources.

    Subclass and set :attr:`endpoints` / :attr:`field_map` for a hand-written
    family, or leave them empty and supply a ``config`` dict on the source row
    for the generic-REST / AI-mapping path::

        source_row["config"] = {
            "endpoints": [{"name": "awards", "path": "/awards",
                           "records_path": "results", "max_pages": 5,
                           "page_param": "page"}],
            "field_map": {"award_id": "id", "buyer": "buyer.name",
                          "value_usd": "amount.amount"},
            "field_types": {"value_usd": "float"},
        }
    """

    #: Declarative endpoints (subclass override; or via source_row["config"]).
    endpoints: list[EndpointSpec] = []
    #: ``{column: "json.path"}`` applied per record by :meth:`normalize`.
    field_map: dict[str, str] = {}
    #: Optional ``{column: caster}`` type coercion for the field map.
    field_types: dict[str, str] = {}

    def __init__(self, source_row: Mapping[str, Any], credential: str | None) -> None:
        super().__init__(source_row, credential)
        cfg = source_row.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except json.JSONDecodeError:
                cfg = {}
        # Runtime config (generic-REST) takes priority over class attributes.
        raw_endpoints = cfg.get("endpoints") if cfg else None
        if raw_endpoints:
            self.endpoints = [EndpointSpec.from_dict(e) for e in raw_endpoints]
        elif self.endpoints and isinstance(self.endpoints[0], Mapping):
            self.endpoints = [EndpointSpec.from_dict(e) for e in self.endpoints]  # type: ignore[arg-type]
        self.field_map = dict(cfg.get("field_map") or self.field_map)
        self.field_types = dict(cfg.get("field_types") or self.field_types)
        self._auth_header = cfg.get("auth_header") if cfg else None  # e.g. "Authorization"
        self._auth_template = cfg.get("auth_template") if cfg else None  # e.g. "Bearer {key}"
        self._auth_query = cfg.get("auth_query") if cfg else None  # e.g. "api_key"

    # ------------------------------------------------------------------ #
    # Request shaping
    # ------------------------------------------------------------------ #
    def default_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.credential and self._auth_header:
            tmpl = self._auth_template or "{key}"
            headers[self._auth_header] = tmpl.format(key=self.credential)
        return headers

    def _auth_params(self) -> dict[str, str]:
        if self.credential and self._auth_query:
            return {self._auth_query: self.credential}
        return {}

    def _render(self, value: Any, ctx: Mapping[str, Any]) -> Any:
        """Fill ``{placeholders}`` in str params/paths from the pull context."""
        if isinstance(value, str):
            try:
                return value.format(**ctx)
            except (KeyError, IndexError):
                return value
        if isinstance(value, Mapping):
            return {k: self._render(v, ctx) for k, v in value.items()}
        if isinstance(value, list):
            return [self._render(v, ctx) for v in value]
        return value

    def _context(self, taxonomy: list[dict[str, Any]],
                 geographies: list[dict[str, Any]], since: str | None) -> dict[str, Any]:
        """Base template context available to endpoint params/paths."""
        return {
            "since": since or "",
            "source_id": self.source_id,
            "taxonomy": taxonomy,
            "geographies": geographies,
        }

    # ------------------------------------------------------------------ #
    # probe
    # ------------------------------------------------------------------ #
    def probe(self) -> ProbeResult:
        """Probe the first endpoint with a tiny request and classify the result."""
        if self.missing_credential():
            return self.auth_failed_probe()
        if not self.endpoints:
            return ProbeResult(
                "SCHEMA_MISMATCH",
                "no endpoints configured (set class attr or source_row['config'])",
                None,
            )
        ep = self.endpoints[0]
        ctx = self._context([], [], None)
        params = self._render(dict(ep.params), ctx)
        params.update(self._auth_params())
        # Ask for the smallest possible page on probe when pagination is defined.
        if ep.page_size_param:
            params.setdefault(ep.page_size_param, 1)
        try:
            resp = self.http.request(
                ep.method, self._render(ep.path, ctx),
                params=params or None,
                headers=ep.headers or None,
                json=self._render(ep.json_body, ctx) if ep.method == "POST" else None,
            )
        except Exception as exc:  # noqa: BLE001
            status, detail = classify_exception(exc)
            return ProbeResult(status, detail, None)

        if resp.status_code >= 300:
            return self.probe_via(resp)
        # 2xx — confirm we can actually find records where we expect them.
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return ProbeResult("SCHEMA_MISMATCH",
                               "200 OK but response is not valid JSON", None)
        records = self._extract_records(payload, ep)
        if not records:
            return ProbeResult("EMPTY",
                               f"reachable, but no records at "
                               f"records_path={ep.records_path!r}", None)
        return ProbeResult("OK", f"HTTP 200, {len(records)} record(s) on probe",
                           records[0])

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
        """Iterate every configured endpoint (+ pages) yielding verbatim records."""
        if self.missing_credential():
            logger.info("%s: missing credential — yielding nothing", self.source_id)
            return
        if not self.endpoints:
            logger.warning("%s: no endpoints configured — yielding nothing", self.source_id)
            return
        ctx = self._context(taxonomy, geographies, since)
        for ep in self.endpoints:
            yield from self._pull_endpoint(ep, ctx)

    def _pull_endpoint(self, ep: EndpointSpec,
                       ctx: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        page = ep.start_page
        pages_done = 0
        while pages_done < max(1, ep.max_pages):
            params = self._render(dict(ep.params), ctx)
            params.update(self._auth_params())
            if ep.page_param:
                params[ep.page_param] = page
            if ep.page_size_param:
                params.setdefault(ep.page_size_param, ep.page_size)
            try:
                resp = self.http.request(
                    ep.method, self._render(ep.path, ctx),
                    params=params or None,
                    headers=ep.headers or None,
                    json=self._render(ep.json_body, ctx) if ep.method == "POST" else None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s endpoint %s request failed: %s",
                               self.source_id, ep.name, exc)
                return
            if resp.status_code >= 300:
                logger.info("%s endpoint %s -> HTTP %s; stopping",
                            self.source_id, ep.name, resp.status_code)
                return
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                logger.warning("%s endpoint %s: non-JSON body", self.source_id, ep.name)
                return
            records = self._extract_records(payload, ep)
            if not records:
                return
            for rec in records:
                if isinstance(rec, Mapping):
                    tagged = dict(rec)
                    if ep.record_tag:
                        tagged.setdefault("_endpoint", ep.name)
                        tagged.update(ep.record_tag)
                    yield tagged
                else:
                    yield {"_value": rec, "_endpoint": ep.name}
            pages_done += 1
            page += 1
            if len(records) < ep.page_size and ep.page_param:
                return  # short page => last page

    @staticmethod
    def _extract_records(payload: Any, ep: EndpointSpec) -> list[Any]:
        """Locate the record list inside a response per the endpoint's config."""
        if ep.records_path:
            found = resolve_path(payload, ep.records_path)
            if isinstance(found, list):
                return found
            if found is None:
                return []
            return [found]
        if isinstance(payload, list):
            return payload
        if isinstance(payload, Mapping):
            return [payload]
        return []

    # ------------------------------------------------------------------ #
    # normalize
    # ------------------------------------------------------------------ #
    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Apply the declarative field map to one verbatim record."""
        if not self.field_map:
            return {}
        return apply_field_map(raw, self.field_map, self.field_types)
