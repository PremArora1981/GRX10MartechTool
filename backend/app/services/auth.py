"""WorkOS AuthKit integration — the backend half of decision Q10.

This service owns everything the API needs to authenticate a principal:

* **Hosted AuthKit login** (``provider=authkit``) which exposes, in one UI, the
  customer's **SSO connections (SAML + OIDC)** *and* the pre-IdP fallbacks
  (email/password + Google). Specific connections/organizations/providers can be
  forced via query params on the login route.
* **Code exchange** against the WorkOS *User Management* API (``httpx``, sync — no
  hard dependency on the ``workos`` Python SDK so the backend degrades gracefully
  when the package or the secrets are absent).
* **Role mapping** — IdP group claims / WorkOS organization-role slugs are mapped
  onto the four product roles (``owner``/``admin`` · ``analyst`` · ``business`` ·
  ``external``). The mapping is data-driven and overridable via ``WORKOS_ROLE_MAP``.
* **Session handling** — a stateless, HMAC-**signed** session cookie keyed by
  ``WORKOS_COOKIE_PASSWORD``. Signing (not plain storage) is what makes the
  ``role`` claim un-forgeable: a user can read their own cookie but cannot tamper
  with it to escalate privilege. The WorkOS ``refresh_token`` rides inside the
  same ``HttpOnly`` cookie so :func:`refresh_login` can mint a fresh session
  without a re-login.

The public entrypoint :func:`resolve_current_user` is what ``deps.py`` discovers
and calls on every request; it performs **no network I/O** (it trusts the signed
cookie until expiry), so it is cheap enough for the hot path.

Security invariants honoured:
* the cookie is ``HttpOnly`` + ``Secure`` (in prod) + ``SameSite`` configurable;
* ``role`` is computed from verified WorkOS/IdP claims, never from client input;
* nothing here fabricates an authenticated user in production — a missing secret
  or a bad cookie resolves to *unauthenticated* (the dependency then returns 401).

----------------------------------------------------------------------------
Environment variables (set in Render / ``.env`` — see ``.env.example``)
----------------------------------------------------------------------------
================================  ==========================================
Variable                          Purpose
================================  ==========================================
``WORKOS_API_KEY``                Secret API key (``sk_...``) used as the
                                  ``client_secret`` for code/refresh exchange.
``WORKOS_CLIENT_ID``              Public client id (``client_...``).
``WORKOS_COOKIE_PASSWORD``        >=32-char secret; HMAC key for the session
                                  cookie *and* the OAuth ``state`` signature.
``WORKOS_REDIRECT_URI``           The registered callback. For this
                                  backend-driven flow it must point at the API's
                                  ``/auth/callback`` (e.g.
                                  ``https://api.example.com/auth/callback``).
                                  If unset, the callback URL is derived from the
                                  incoming request.
``WORKOS_SESSION_TTL_SECONDS``    Optional. Lifetime of the signed session
                                  cookie (default 43200 = 12h).
``WORKOS_ROLE_MAP``               Optional JSON object overriding/extending the
                                  group/role-slug -> product-role mapping, e.g.
                                  ``{"market-research-admins":"admin"}``.
``SESSION_COOKIE_SAMESITE``       Optional. ``lax`` (default) | ``strict`` |
                                  ``none``. Use ``none`` only when the frontend
                                  and API are on different *sites* (also forces
                                  ``Secure``).
================================  ==========================================
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlencode

import httpx
from fastapi import Request

from backend.app.config import settings
from backend.app.schemas import CurrentUser, Role

logger = logging.getLogger("grx10.auth")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
WORKOS_API_BASE = "https://api.workos.com"
AUTHORIZE_PATH = "/user_management/authorize"
AUTHENTICATE_PATH = "/user_management/authenticate"
LOGOUT_PATH = "/user_management/sessions/logout"

#: Name of the signed session cookie set after a successful login.
SESSION_COOKIE_NAME = "grx10_session"
#: Short-lived signed cookie that carries the OAuth ``state`` / CSRF nonce.
STATE_COOKIE_NAME = "grx10_oauth_state"

_DEFAULT_SESSION_TTL = 12 * 60 * 60  # 12 hours
_STATE_TTL = 10 * 60  # 10 minutes — login round-trip window
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

#: Dev fallback principal used ONLY when WorkOS is unconfigured outside prod, so
#: the app stays runnable end-to-end before the IdP is provisioned. Mirrors the
#: anonymous owner that ``deps.py`` would otherwise use when no auth service
#: exists. Never returned when ``settings.auth_configured`` is true.
_DEV_USER = CurrentUser(
    id="dev-anonymous",
    email="dev@localhost",
    first_name="Dev",
    last_name="User",
    role="owner",
)


# --------------------------------------------------------------------------- #
# Role mapping (IdP group claims / WorkOS org-role slugs -> product roles)
# --------------------------------------------------------------------------- #
#: Most-privileged first; used to pick the strongest role when a principal
#: carries several matching group claims.
_ROLE_PRECEDENCE: tuple[Role, ...] = ("owner", "admin", "analyst", "business", "external")

#: Built-in mapping from a lower-cased claim token to a product role. Extended /
#: overridden at import time by the ``WORKOS_ROLE_MAP`` JSON env var.
_DEFAULT_ROLE_MAP: dict[str, Role] = {
    # owner / admin (gates credential entry — Q9 — and the audience switcher)
    "owner": "owner",
    "owners": "owner",
    "org_owner": "owner",
    "org_owners": "owner",
    "superadmin": "owner",
    "admin": "admin",
    "admins": "admin",
    "administrator": "admin",
    "administrators": "admin",
    "org_admin": "admin",
    "org_admins": "admin",
    "owner/admin": "admin",
    # analyst
    "analyst": "analyst",
    "analysts": "analyst",
    "research": "analyst",
    "researcher": "analyst",
    # business / read-mostly
    "business": "business",
    "member": "business",
    "members": "business",
    "viewer": "business",
    "user": "business",
    "stakeholder": "business",
    # external / least privilege
    "external": "external",
    "guest": "external",
}


def _load_role_map() -> dict[str, Role]:
    """Return the effective claim->role map (defaults + ``WORKOS_ROLE_MAP`` JSON)."""
    mapping: dict[str, Role] = dict(_DEFAULT_ROLE_MAP)
    raw = os.environ.get("WORKOS_ROLE_MAP", "").strip()
    if not raw:
        return mapping
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("WORKOS_ROLE_MAP is not valid JSON, ignoring: %s", exc)
        return mapping
    valid = set(_ROLE_PRECEDENCE)
    for key, value in overrides.items():
        if value in valid:
            mapping[str(key).strip().lower()] = value  # type: ignore[assignment]
        else:
            logger.warning("WORKOS_ROLE_MAP value %r for %r is not a known role", value, key)
    return mapping


_ROLE_MAP: dict[str, Role] = _load_role_map()


def map_role(role_slug: str | None, groups: Iterable[str] | None = None) -> Role:
    """Map a WorkOS org-role slug and/or IdP group claims to a product role.

    Resolution order:

    1. an explicit WorkOS organization **role slug** (set in the WorkOS dashboard,
       which is itself where SAML/OIDC group->role rules are configured);
    2. otherwise the **IdP group claims** passed straight through from the
       connection, taking the most-privileged match;
    3. otherwise ``external`` (safe default — least privilege).

    Matching is case-insensitive and tolerant of separators (``Org Admins`` and
    ``org_admins`` both resolve via ``org_admins`` / ``admins``).
    """
    candidates: list[str] = []
    if role_slug:
        candidates.append(role_slug)
    if groups:
        candidates.extend(g for g in groups if g)

    best: Role | None = None
    for token in candidates:
        mapped = _ROLE_MAP.get(_normalize_token(token))
        if mapped is None:
            continue
        if best is None or _ROLE_PRECEDENCE.index(mapped) < _ROLE_PRECEDENCE.index(best):
            best = mapped
    return best or "external"


def _normalize_token(token: str) -> str:
    """Lower-case and collapse separators so IdP group labels match map keys."""
    return token.strip().lower().replace(" ", "_").replace("-", "_")


# --------------------------------------------------------------------------- #
# Signing primitives (stdlib only — no extra crypto dependency)
# --------------------------------------------------------------------------- #
def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _signing_key() -> bytes:
    """HMAC key derived from ``WORKOS_COOKIE_PASSWORD`` (raises if unconfigured)."""
    secret = settings.WORKOS_COOKIE_PASSWORD
    if not secret:
        raise RuntimeError("WORKOS_COOKIE_PASSWORD is required to sign sessions.")
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _sign(payload: dict[str, Any]) -> str:
    """Serialize ``payload`` to a tamper-evident ``<body>.<hmac>`` token."""
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    mac = hmac.new(_signing_key(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(mac)}"


def _unsign(token: str | None) -> dict[str, Any] | None:
    """Verify and decode a token produced by :func:`_sign`; ``None`` if invalid.

    Verifies the HMAC in constant time and enforces the ``exp`` epoch-seconds
    claim when present. Any malformation -> ``None`` (treated as no session).
    """
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    try:
        expected = hmac.new(_signing_key(), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(sig)):
            return None
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, RuntimeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and exp < time.time():
        return None
    return payload


# Public, intention-revealing aliases used by the router.
def seal_session(payload: dict[str, Any]) -> str:
    """Sign a session payload into the value for :data:`SESSION_COOKIE_NAME`."""
    return _sign(payload)


def unseal_session(token: str | None) -> dict[str, Any] | None:
    """Verify a session cookie value; ``None`` if missing/tampered/expired."""
    return _unsign(token)


def sign_state(return_to: str | None) -> str:
    """Create a signed OAuth ``state`` carrying a CSRF nonce + post-login target."""
    return _sign(
        {
            "nonce": _b64encode(os.urandom(16)),
            "return_to": return_to or "",
            "exp": int(time.time()) + _STATE_TTL,
        }
    )


def verify_state(state: str | None) -> dict[str, Any] | None:
    """Verify the ``state`` returned to the callback; ``None`` if invalid/expired."""
    return _unsign(state)


# --------------------------------------------------------------------------- #
# Session payload / cookie helpers
# --------------------------------------------------------------------------- #
def session_ttl_seconds() -> int:
    """Configured session lifetime in seconds (``WORKOS_SESSION_TTL_SECONDS``)."""
    raw = os.environ.get("WORKOS_SESSION_TTL_SECONDS", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("WORKOS_SESSION_TTL_SECONDS is not an int, using default")
    return _DEFAULT_SESSION_TTL


def cookie_params() -> dict[str, Any]:
    """Keyword args for ``Response.set_cookie`` for the session cookie.

    ``HttpOnly`` always (no JS access); ``Secure`` in production and whenever
    ``SameSite=None`` is selected (browsers reject ``None`` without ``Secure``).
    """
    samesite = os.environ.get("SESSION_COOKIE_SAMESITE", "lax").strip().lower()
    if samesite not in ("lax", "strict", "none"):
        samesite = "lax"
    secure = settings.ENV == "production" or samesite == "none"
    return {
        "key": SESSION_COOKIE_NAME,
        "httponly": True,
        "secure": secure,
        "samesite": samesite,
        "path": "/",
        "max_age": session_ttl_seconds(),
    }


@dataclass(slots=True)
class LoginResult:
    """Outcome of a successful code/refresh exchange."""

    user: CurrentUser
    session_payload: dict[str, Any]  # -> seal_session() -> cookie value
    workos_session_id: str | None    # for the WorkOS logout endpoint


def _build_session_payload(
    *, user: CurrentUser, workos_session_id: str | None, refresh_token: str | None
) -> dict[str, Any]:
    """Assemble the (about-to-be-signed) session payload from resolved identity."""
    now = int(time.time())
    return {
        "sub": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "role": user.role,
        "org": user.organization_id,
        "sid": workos_session_id,
        "rt": refresh_token,  # WorkOS refresh token — enables /auth/refresh
        "iat": now,
        "exp": now + session_ttl_seconds(),
    }


def _current_user_from_payload(payload: dict[str, Any]) -> CurrentUser:
    """Reconstruct the :class:`CurrentUser` from a verified session payload."""
    role = payload.get("role")
    if role not in _ROLE_PRECEDENCE:
        role = "external"
    return CurrentUser(
        id=str(payload.get("sub") or "unknown"),
        email=payload.get("email"),
        first_name=payload.get("first_name"),
        last_name=payload.get("last_name"),
        role=role,  # type: ignore[arg-type]
        organization_id=payload.get("org"),
    )


# --------------------------------------------------------------------------- #
# JWT helpers (decode only — the token arrives over TLS straight from WorkOS)
# --------------------------------------------------------------------------- #
def _decode_jwt_claims(token: str | None) -> dict[str, Any]:
    """Best-effort decode of a JWT *payload* (no signature verification).

    The access token is received directly from the WorkOS API over TLS, so it is
    already trusted for this request; we only need to read its claims (``sid``,
    ``role``, ``permissions``, ``org_id``, IdP ``groups``). Returns ``{}`` on any
    malformation rather than raising.
    """
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        claims = json.loads(_b64decode(parts[1]).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _extract_groups(*sources: dict[str, Any]) -> list[str]:
    """Collect IdP group/role-ish string claims from any of the given dicts."""
    out: list[str] = []
    for src in sources:
        for key in ("groups", "roles", "memberships"):
            val = src.get(key)
            if isinstance(val, str):
                out.append(val)
            elif isinstance(val, list):
                out.extend(str(v) for v in val if isinstance(v, (str, int)))
    return out


def _resolve_role(auth_response: dict[str, Any], claims: dict[str, Any]) -> Role:
    """Derive the product role from a WorkOS authenticate response + JWT claims."""
    # WorkOS organization role slug: in the JWT (`role`) and/or response body.
    role_obj = auth_response.get("role")
    role_slug: str | None = None
    if isinstance(role_obj, dict):
        role_slug = role_obj.get("slug")
    elif isinstance(role_obj, str):
        role_slug = role_obj
    role_slug = role_slug or claims.get("role")
    groups = _extract_groups(claims, auth_response, auth_response.get("user") or {})
    return map_role(role_slug, groups)


# --------------------------------------------------------------------------- #
# WorkOS API calls (httpx, sync)
# --------------------------------------------------------------------------- #
class WorkOSError(RuntimeError):
    """Raised when a WorkOS API call fails; carries an HTTP-ish status hint."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def _require_configured() -> None:
    if not settings.auth_configured:
        raise WorkOSError("WorkOS auth is not configured on this server.", status_code=503)


def authorization_url(
    *,
    redirect_uri: str,
    state: str,
    provider: str | None = "authkit",
    connection_id: str | None = None,
    organization_id: str | None = None,
    login_hint: str | None = None,
    screen_hint: str | None = None,
) -> str:
    """Build the WorkOS hosted authorization URL to redirect the browser to.

    With no overrides this uses ``provider=authkit`` — the hosted UI that offers
    the customer's configured SSO connections (SAML + OIDC) **and** the
    email/password + Google fallbacks in one screen. Callers may instead force a
    specific ``connection_id`` (one SSO connection), ``organization_id`` (an org's
    connection), or ``provider`` (e.g. ``GoogleOAuth``). Exactly one selector is
    sent; ``connection_id``/``organization_id`` take precedence over ``provider``.
    """
    _require_configured()
    params: dict[str, str] = {
        "client_id": settings.WORKOS_CLIENT_ID or "",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if connection_id:
        params["connection_id"] = connection_id
    elif organization_id:
        params["organization_id"] = organization_id
    elif provider:
        params["provider"] = provider
    if login_hint:
        params["login_hint"] = login_hint
    if screen_hint:
        params["screen_hint"] = screen_hint
    return f"{WORKOS_API_BASE}{AUTHORIZE_PATH}?{urlencode(params)}"


def _authenticate(extra: dict[str, Any]) -> LoginResult:
    """Shared code/refresh-token exchange against ``/authenticate``."""
    _require_configured()
    body = {
        "client_id": settings.WORKOS_CLIENT_ID,
        "client_secret": settings.WORKOS_API_KEY,
        **extra,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(f"{WORKOS_API_BASE}{AUTHENTICATE_PATH}", json=body)
    except httpx.HTTPError as exc:
        raise WorkOSError(f"Could not reach WorkOS: {exc}", status_code=502) from exc

    if resp.status_code >= 400:
        detail = _safe_error_detail(resp)
        # 400/401 here generally means a bad/expired code or invalid client creds.
        status = 401 if resp.status_code in (400, 401) else 502
        raise WorkOSError(f"WorkOS authentication failed: {detail}", status_code=status)

    data = resp.json()
    claims = _decode_jwt_claims(data.get("access_token"))
    user_obj = data.get("user") or {}
    role = _resolve_role(data, claims)
    user = CurrentUser(
        id=str(user_obj.get("id") or claims.get("sub") or "unknown"),
        email=user_obj.get("email"),
        first_name=user_obj.get("first_name"),
        last_name=user_obj.get("last_name"),
        role=role,
        organization_id=data.get("organization_id") or claims.get("org_id"),
    )
    workos_sid = claims.get("sid") or data.get("session_id")
    payload = _build_session_payload(
        user=user,
        workos_session_id=workos_sid,
        refresh_token=data.get("refresh_token"),
    )
    logger.info("authenticated user %s (role=%s, org=%s)", user.email, user.role, user.organization_id)
    return LoginResult(user=user, session_payload=payload, workos_session_id=workos_sid)


def complete_login(*, code: str, redirect_uri: str) -> LoginResult:
    """Exchange an authorization ``code`` (from the callback) for a session."""
    return _authenticate({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})


def refresh_login(*, refresh_token: str) -> LoginResult:
    """Mint a fresh session from a stored WorkOS ``refresh_token``."""
    return _authenticate({"grant_type": "refresh_token", "refresh_token": refresh_token})


def logout_url(*, session_id: str | None, return_to: str | None = None) -> str:
    """WorkOS hosted-logout URL that ends the IdP session then returns to the app.

    Falls back to ``return_to`` (or the app URL) when there is no WorkOS session
    id to terminate (e.g. a stale local cookie), so logout always lands somewhere
    sensible.
    """
    target = return_to or settings.NEXT_PUBLIC_APP_URL
    if not session_id:
        return target
    params = {"session_id": session_id}
    if return_to:
        params["return_to"] = return_to
    return f"{WORKOS_API_BASE}{LOGOUT_PATH}?{urlencode(params)}"


def _safe_error_detail(resp: httpx.Response) -> str:
    """Extract a human-readable error message from a WorkOS error response."""
    try:
        data = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    for key in ("error_description", "message", "error"):
        if isinstance(data.get(key), str):
            return f"{resp.status_code}: {data[key]}"
    return f"HTTP {resp.status_code}"


# --------------------------------------------------------------------------- #
# The entrypoint deps.py discovers and calls on every request
# --------------------------------------------------------------------------- #
def resolve_current_user(request: Request) -> CurrentUser | None:
    """Resolve the request principal from the signed session cookie.

    Contract expected by ``backend.app.deps``:

    * return a :class:`CurrentUser` for an authenticated request;
    * return ``None`` for an unauthenticated one (the dependency turns that into
      ``401`` in production);
    * **never** raise for an absent/invalid cookie — only genuinely exceptional
      states should propagate.

    Performs no network I/O: the HMAC-signed cookie is self-validating until its
    ``exp``. When WorkOS is unconfigured outside production, returns a dev
    ``owner`` so the stack is runnable before the IdP exists; in production an
    unconfigured server resolves to ``None`` (fail closed).
    """
    if not settings.auth_configured:
        if settings.ENV == "production":
            return None
        logger.debug("WorkOS unconfigured (dev) — resolving anonymous owner")
        return _DEV_USER

    payload = unseal_session(request.cookies.get(SESSION_COOKIE_NAME))
    if payload is None:
        return None
    return _current_user_from_payload(payload)


# Alias kept so ``deps._resolve_auth_service`` finds an entrypoint under any of
# its conventional names.
get_current_user = resolve_current_user
current_user_from_request = resolve_current_user


__all__ = [
    "SESSION_COOKIE_NAME",
    "STATE_COOKIE_NAME",
    "LoginResult",
    "WorkOSError",
    "map_role",
    "authorization_url",
    "complete_login",
    "refresh_login",
    "logout_url",
    "seal_session",
    "unseal_session",
    "sign_state",
    "verify_state",
    "cookie_params",
    "session_ttl_seconds",
    "resolve_current_user",
    "get_current_user",
    "current_user_from_request",
]
