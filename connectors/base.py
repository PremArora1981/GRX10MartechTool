"""Connector framework — the plug-in contract every data source implements.

A *connector* knows how to talk to one external source and lands its data,
verbatim, into exactly one ``raw_*`` table. The pipeline (``pipeline/run.py``)
drives connectors generically through three methods:

    probe()    -> ProbeResult        cheap reachability/auth test (7-state taxonomy)
    pull(...)  -> Iterator[dict]      yields verbatim source payloads
    normalize(raw) -> dict            maps one verbatim payload to typed columns

Invariants enforced here (from the v1 spec):

* **Never fabricate data.** When a required credential is missing, :meth:`probe`
  returns ``AUTH_FAILED`` and :meth:`pull` yields nothing — the connector must
  not invent rows.
* **Every error is classified** into the 7-state health taxonomy
  (``OK | AUTH_FAILED | QUOTA_EXHAUSTED | RATE_LIMITED | UNREACHABLE |
  SCHEMA_MISMATCH | EMPTY``) via :func:`classify_http_error`, so the Connectors
  admin UI and the pipeline's ``probe_health`` stage always get a typed status.

Connector subclasses set the class attributes ``source_id`` and ``raw_table``
(or rely on the values carried in the seeded ``sources`` row, which the base
class copies onto the instance in :meth:`__init__`).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator, Literal, Mapping

import httpx

from connectors.http import DEFAULT_MIN_INTERVAL, HttpClient

logger = logging.getLogger("grx10.connectors")

# The 7-state connector-health taxonomy (v1-definition Q7).
ProbeStatus = Literal[
    "OK",
    "AUTH_FAILED",
    "QUOTA_EXHAUSTED",
    "RATE_LIMITED",
    "UNREACHABLE",
    "SCHEMA_MISMATCH",
    "EMPTY",
]

# Keyword hints used to disambiguate ambiguous 4xx bodies (e.g. a 403 that is
# really an exhausted credit balance, or a 400 that is really over-quota).
_QUOTA_HINTS = (
    "quota", "credit", "out of credit", "insufficient fund", "payment required",
    "billing", "plan limit", "usage limit", "limit exceeded", "exceeded your",
    "monthly limit", "daily limit", "subscription", "upgrade your",
)
_RATE_HINTS = (
    "rate limit", "too many request", "throttl", "slow down", "try again later",
    "requests per", "calls per",
)
_AUTH_HINTS = (
    "unauthorized", "forbidden", "invalid api key", "invalid key", "api key",
    "authentication", "auth failed", "access denied", "not authorized",
    "invalid token", "expired token", "permission",
)


def classify_http_error(status_code: int, body: str | bytes | None = None) -> ProbeStatus:
    """Map an HTTP status (+ optional body) to the 7-state health taxonomy.

    The base mapping follows the spec:

    * ``402`` (and credit/quota wording in the body) -> ``QUOTA_EXHAUSTED``
    * ``429`` -> ``RATE_LIMITED``
    * ``401`` / ``403`` -> ``AUTH_FAILED`` (unless the body clearly says the
      account is out of quota/credit, in which case ``QUOTA_EXHAUSTED``)
    * ``400`` / ``404`` / ``409`` / ``410`` / ``422`` -> ``SCHEMA_MISMATCH``
      (bad request shape, moved/removed endpoint, unparseable contract)
    * ``5xx`` and other unhandled codes -> ``UNREACHABLE``

    The body text is scanned for quota/rate/auth hints to refine genuinely
    ambiguous statuses (many free APIs signal "out of credit" with a 403).
    """
    text = ""
    if body is not None:
        text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else str(body)
    text_l = text.lower()

    def _mentions(hints: tuple[str, ...]) -> bool:
        return any(h in text_l for h in hints)

    if status_code == 402:
        return "QUOTA_EXHAUSTED"
    if status_code == 429:
        # A 429 occasionally fronts a hard quota wall rather than a soft throttle.
        return "QUOTA_EXHAUSTED" if _mentions(_QUOTA_HINTS) else "RATE_LIMITED"
    if status_code in (401, 403):
        if _mentions(_QUOTA_HINTS):
            return "QUOTA_EXHAUSTED"
        if _mentions(_RATE_HINTS):
            return "RATE_LIMITED"
        return "AUTH_FAILED"
    if status_code in (400, 404, 405, 406, 409, 410, 415, 422, 451):
        # Disambiguate the occasional quota/auth message delivered with a 400.
        if _mentions(_QUOTA_HINTS):
            return "QUOTA_EXHAUSTED"
        if _mentions(_AUTH_HINTS):
            return "AUTH_FAILED"
        return "SCHEMA_MISMATCH"
    if status_code >= 500:
        return "UNREACHABLE"
    if 200 <= status_code < 300:
        return "OK"
    # Anything else (3xx left unfollowed, 1xx, unknown) -> treat as unreachable.
    return "UNREACHABLE"


def classify_exception(exc: Exception) -> tuple[ProbeStatus, str]:
    """Map a raised transport-level exception to a status + human detail."""
    if isinstance(exc, httpx.TimeoutException):
        return "UNREACHABLE", f"timeout: {exc}"
    if isinstance(exc, httpx.TransportError):
        return "UNREACHABLE", f"transport error: {exc}"
    return "UNREACHABLE", f"{type(exc).__name__}: {exc}"


@dataclass
class ProbeResult:
    """Outcome of a connector health probe.

    Attributes
    ----------
    status:
        One of the 7 :data:`ProbeStatus` values.
    detail:
        Human-readable explanation (surfaced in the Connectors admin UI and the
        ``sources.last_probe_detail`` column).
    sample:
        An optional small sample payload proving the probe actually reached data
        (``None`` for failures).
    """

    status: ProbeStatus
    detail: str
    sample: Any | None = None

    @property
    def ok(self) -> bool:
        return self.status == "OK"


class Connector(ABC):
    """Abstract base class for all source connectors.

    Subclasses MUST implement :meth:`probe`, :meth:`pull`, and :meth:`normalize`
    and SHOULD set the class attributes :attr:`source_id` and :attr:`raw_table`
    (the seeded ``sources`` row supplies them at runtime otherwise).
    """

    #: Stable identifier matching ``sources.source_id`` (set by subclass or row).
    source_id: str = ""
    #: Destination ``raw_*`` table (set by subclass or carried in the row).
    raw_table: str = ""
    #: Polite throttle (seconds between requests) — override per source.
    min_interval: float = DEFAULT_MIN_INTERVAL

    def __init__(self, source_row: Mapping[str, Any], credential: str | None) -> None:
        """Bind the connector to one seeded source row + its (optional) secret.

        ``source_row`` is the dict the pipeline reads from the ``sources`` table
        (note it aliases the reserved column ``class`` to ``source_class``).
        ``credential`` is the already-decrypted secret (or ``None`` when the
        source needs no auth, or the secret is unavailable).
        """
        self.source_row: dict[str, Any] = dict(source_row)
        self.credential = credential
        # Prefer the row's identity; fall back to the class attribute.
        self.source_id = str(source_row.get("source_id") or self.source_id)
        self.raw_table = str(source_row.get("raw_table") or self.raw_table)
        self.base_url: str | None = source_row.get("url_pattern") or None
        self.source_class: str | None = (
            source_row.get("source_class") or source_row.get("class")
        )
        self._http: HttpClient | None = None

    # ------------------------------------------------------------------ #
    # Shared HTTP client (lazy; reused across probe/pull)
    # ------------------------------------------------------------------ #
    @property
    def http(self) -> HttpClient:
        """A lazily-built, throttled HTTP client scoped to this source."""
        if self._http is None:
            self._http = HttpClient(
                base_url=self.base_url,
                min_interval=self.min_interval,
                headers=self.default_headers(),
            )
        return self._http

    def default_headers(self) -> dict[str, str]:
        """Per-source default headers. Override to add ``Origin``/``Referer`` etc."""
        return {}

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    # ------------------------------------------------------------------ #
    # Credential helpers — never fabricate; missing key => AUTH_FAILED
    # ------------------------------------------------------------------ #
    def requires_auth(self) -> bool:
        """True when the seeded source declares an auth method other than ``none``."""
        auth = (self.source_row.get("auth") or "none").lower()
        return auth not in ("none", "", "scrape")

    def missing_credential(self) -> bool:
        """True when auth is required but no decrypted credential was supplied."""
        return self.requires_auth() and not self.credential

    def auth_failed_probe(self, detail: str | None = None) -> ProbeResult:
        """Standard ``AUTH_FAILED`` result for a missing/invalid credential."""
        return ProbeResult(
            "AUTH_FAILED",
            detail or f"no credential available for {self.source_id} "
            f"(auth={self.source_row.get('auth')})",
            None,
        )

    # ------------------------------------------------------------------ #
    # Budget / quota pre-warning (Q7) — advisory, computed by callers
    # ------------------------------------------------------------------ #
    def budget_warning(self, *, spent: float | None = None,
                       used_calls: int | None = None) -> str | None:
        """Return an advisory string when usage nears the configured ceiling.

        Fires at ~80% of ``monthly_budget`` or ``quota_ceiling`` (the spec's 🟠
        pre-warning) so "out of money" is caught before it blocks ingestion.
        """
        warnings: list[str] = []
        budget = self.source_row.get("monthly_budget")
        if budget and spent is not None and budget > 0 and spent >= 0.8 * float(budget):
            warnings.append(f"spend {spent:.2f} of {float(budget):.2f} budget")
        ceiling = self.source_row.get("quota_ceiling")
        if ceiling and used_calls is not None and ceiling > 0 \
                and used_calls >= 0.8 * int(ceiling):
            warnings.append(f"{used_calls} of {int(ceiling)} calls")
        return "; ".join(warnings) if warnings else None

    # ------------------------------------------------------------------ #
    # The contract (must match pipeline expectations EXACTLY)
    # ------------------------------------------------------------------ #
    @abstractmethod
    def probe(self) -> ProbeResult:
        """Cheap test call -> a :class:`ProbeResult` in the 7-state taxonomy."""
        raise NotImplementedError

    @abstractmethod
    def pull(
        self,
        *,
        taxonomy: list[dict[str, Any]],
        geographies: list[dict[str, Any]],
        since: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield verbatim source payloads (dicts) for the given scope.

        ``taxonomy`` / ``geographies`` are the active spine rows; ``since`` is an
        ISO-date lookback hint. Yields nothing when the source is unreachable or
        unauthorised — it must NEVER fabricate rows.
        """
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map one verbatim payload to typed columns of :attr:`raw_table`.

        Returns a dict whose keys are a subset of the destination table's typed
        columns. The pipeline only writes keys that match real columns, so extra
        keys are harmless. Return ``{}`` when nothing can be typed.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Convenience for subclasses
    # ------------------------------------------------------------------ #
    def probe_via(self, response: httpx.Response, *,
                  empty_when: bool = False) -> ProbeResult:
        """Build a :class:`ProbeResult` from a probe response.

        ``empty_when`` lets a caller flag a 200-but-no-data condition as
        ``EMPTY`` (a valid 7-state value distinct from ``OK``).
        """
        if 200 <= response.status_code < 300:
            if empty_when:
                return ProbeResult("EMPTY", "reachable but returned no records", None)
            sample = self._safe_sample(response)
            return ProbeResult("OK", f"HTTP {response.status_code}", sample)
        status = classify_http_error(response.status_code, response.text)
        return ProbeResult(status, f"HTTP {response.status_code}: "
                           f"{response.text[:200]}", None)

    @staticmethod
    def _safe_sample(response: httpx.Response, *, limit: int = 500) -> Any:
        """Return a tiny sample of the response for the probe (best-effort)."""
        try:
            data = response.json()
        except Exception:  # noqa: BLE001 — non-JSON body
            return response.text[:limit]
        if isinstance(data, list):
            return data[:1]
        if isinstance(data, dict):
            return {k: data[k] for k in list(data)[:5]}
        return data

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} source_id={self.source_id!r} raw_table={self.raw_table!r}>"
