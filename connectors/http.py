"""Shared HTTP client for connectors.

A thin, defensive wrapper around :mod:`httpx` that every connector uses for its
network I/O so the whole connector layer behaves consistently:

* **Retry / backoff** on transient failures (connection errors, timeouts, and the
  retryable status codes 429/502/503/504) using :mod:`tenacity` with exponential
  backoff + jitter, and honouring an upstream ``Retry-After`` header when present.
  If ``tenacity`` is not installed the client degrades to an equivalent built-in
  retry loop (logged once) rather than failing to import.
* **Polite throttling** — a configurable minimum interval between requests to the
  same client, because most government APIs publish no rate limit and must be
  hit gently (catalog "universal engineering notes").
* **Configurable, descriptive User-Agent** — several sources (SEC EDGAR,
  SingStat, SIA) *require* a descriptive UA or return 403. The UA is read from
  the ``GRX10_USER_AGENT`` env var with a sensible, contactable default and can
  be overridden per client.

The client never raises for ordinary HTTP error statuses — it returns the
:class:`httpx.Response` so each connector can map the status into the 7-state
health taxonomy via :func:`connectors.base.classify_http_error`. It only raises
(after exhausting retries) for genuinely transient transport failures.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any, Mapping

import httpx

logger = logging.getLogger("grx10.connectors.http")

# Status codes worth retrying — transient server / throttling conditions.
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_BASE = 0.5          # seconds; doubled each attempt
DEFAULT_BACKOFF_MAX = 30.0          # cap a single backoff wait
DEFAULT_MIN_INTERVAL = 0.0          # polite throttle; per-connector override

# A descriptive, contactable default UA. Gov APIs reject anonymous/blank agents.
DEFAULT_USER_AGENT = os.environ.get(
    "GRX10_USER_AGENT",
    "GRX10-MarketResearch/1.0 (+https://grx10.com; research@grx10.com)",
)


class RetryableHTTPError(Exception):
    """Internal signal that a response status is transient and should be retried.

    Carries the parsed ``Retry-After`` delay (seconds) when the server provided
    one, so the backoff strategy can honour it instead of guessing.
    """

    def __init__(self, status_code: int, retry_after: float | None = None) -> None:
        super().__init__(f"retryable HTTP status {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value (delta-seconds form only) to float."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        # HTTP-date form is rare for these APIs; ignore and fall back to backoff.
        return None


try:  # tenacity is the specified backoff library; degrade gracefully if absent.
    from tenacity import (
        Retrying,
        retry_if_exception_type,
        stop_after_attempt,
    )

    _HAVE_TENACITY = True
except Exception:  # noqa: BLE001
    _HAVE_TENACITY = False
    logger.info("tenacity not installed — using built-in retry fallback")


class HttpClient:
    """A reusable, throttled, retrying HTTP client wrapping :class:`httpx.Client`.

    Parameters
    ----------
    base_url:
        Optional base URL; relative paths passed to :meth:`request` are joined
        onto it (matches a source's ``url_pattern``).
    user_agent:
        Overrides :data:`DEFAULT_USER_AGENT` for sources that demand a specific
        descriptive agent string.
    timeout:
        Per-request timeout in seconds.
    min_interval:
        Minimum seconds between successive requests from this client (polite
        throttling). ``0`` disables throttling.
    headers:
        Extra default headers merged into every request (e.g. ``Origin`` /
        ``Referer`` for undocumented endpoints).
    max_attempts:
        Total attempts (initial + retries) for transient failures.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        user_agent: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        headers: Mapping[str, str] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
    ) -> None:
        default_headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT,
                           "Accept": "application/json, */*"}
        if headers:
            default_headers.update(headers)
        self._client = httpx.Client(
            base_url=base_url or "",
            headers=default_headers,
            timeout=timeout,
            follow_redirects=True,
        )
        self._min_interval = max(0.0, float(min_interval))
        self._max_attempts = max(1, int(max_attempts))
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    # ------------------------------------------------------------------ #
    # Throttling
    # ------------------------------------------------------------------ #
    def _throttle(self) -> None:
        """Block until ``min_interval`` has elapsed since the last request."""
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    # ------------------------------------------------------------------ #
    # Core request with retry/backoff
    # ------------------------------------------------------------------ #
    def _send_once(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Issue a single (throttled) request, flagging retryable statuses."""
        self._throttle()
        response = self._client.request(method, url, **kwargs)
        if response.status_code in RETRYABLE_STATUS:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            # Stash the response on the exception so the caller can still inspect
            # it once retries are exhausted.
            exc = RetryableHTTPError(response.status_code, retry_after)
            exc.response = response  # type: ignore[attr-defined]
            raise exc
        return response

    def _backoff_seconds(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._backoff_max)
        wait = self._backoff_base * (2 ** (attempt - 1))
        return min(wait, self._backoff_max) + random.uniform(0, self._backoff_base)

    def request(self, method: str, url: str = "", **kwargs: Any) -> httpx.Response:
        """Perform an HTTP request with retry/backoff and throttling.

        Returns the :class:`httpx.Response` for *any* completed HTTP exchange —
        including 4xx/5xx — so the connector can classify it. Raises the
        underlying :class:`httpx.TransportError` (or the final
        :class:`RetryableHTTPError`'s response) only when all attempts fail.
        """
        retryable_excs = (httpx.TransportError, RetryableHTTPError)

        if _HAVE_TENACITY:
            last_exc: BaseException | None = None
            try:
                for attempt in Retrying(
                    stop=stop_after_attempt(self._max_attempts),
                    retry=retry_if_exception_type(retryable_excs),
                    wait=self._tenacity_wait,
                    reraise=True,
                ):
                    with attempt:
                        return self._send_once(method, url, **kwargs)
            except RetryableHTTPError as exc:  # exhausted on a retryable status
                resp = getattr(exc, "response", None)
                if resp is not None:
                    return resp  # let the connector classify the final status
                last_exc = exc
            except httpx.TransportError as exc:
                last_exc = exc
            assert last_exc is not None
            raise last_exc

        # ---- Built-in fallback when tenacity is unavailable ----
        last_error: BaseException | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._send_once(method, url, **kwargs)
            except RetryableHTTPError as exc:
                last_error = exc
                if attempt >= self._max_attempts:
                    resp = getattr(exc, "response", None)
                    if resp is not None:
                        return resp
                    raise
                time.sleep(self._backoff_seconds(attempt, exc.retry_after))
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self._max_attempts:
                    raise
                time.sleep(self._backoff_seconds(attempt, None))
        assert last_error is not None
        raise last_error

    def _tenacity_wait(self, retry_state: Any) -> float:
        """tenacity wait strategy honouring ``Retry-After`` then exponential backoff."""
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        retry_after = getattr(exc, "retry_after", None) if exc else None
        return self._backoff_seconds(retry_state.attempt_number, retry_after)

    # ------------------------------------------------------------------ #
    # Convenience verbs
    # ------------------------------------------------------------------ #
    def get(self, url: str = "", **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str = "", **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
