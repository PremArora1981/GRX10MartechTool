"""Authentication routes — WorkOS AuthKit login / callback / logout / session.

The browser-facing half of decision Q10. Flow (backend-driven, self-contained):

1. ``GET  /auth/login``    -> 307 to the WorkOS hosted AuthKit UI (SSO SAML+OIDC
                              plus email/Google fallback). A signed ``state``
                              cookie carries the CSRF nonce + post-login target.
2. ``GET  /auth/callback`` -> WorkOS redirects here with ``code`` + ``state``;
                              we verify ``state``, exchange the code, set the
                              signed ``HttpOnly`` session cookie, and 307 the
                              user back into the app.
3. ``POST /auth/refresh``  -> renew the session from the stored refresh token.
4. ``POST /auth/logout``   -> clear the cookie and redirect through WorkOS logout.
5. ``GET  /auth/me``       -> the current principal (drives the frontend's
                              role-aware UI); ``GET /auth/config`` exposes whether
                              auth is wired without leaking secrets.

This module is auto-discovered by ``main.register_routers`` (it exposes a
module-level ``router``); no edit to ``main.py`` is required.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from backend.app.config import settings
from backend.app.deps import CurrentUserDep
from backend.app.schemas import CurrentUser
from backend.app.services import auth as auth_service

logger = logging.getLogger("grx10.auth.router")

router = APIRouter(prefix="/auth", tags=["auth"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _callback_url(request: Request) -> str:
    """The redirect URI handed to WorkOS — must be identical on authorize+exchange.

    Prefers the explicitly registered ``WORKOS_REDIRECT_URI`` (what the operator
    configured in the WorkOS dashboard). Falls back to deriving the backend's own
    ``/auth/callback`` from the incoming request so local dev works with zero
    config.
    """
    if settings.WORKOS_REDIRECT_URI:
        return settings.WORKOS_REDIRECT_URI
    return str(request.url_for("auth_callback"))


def _safe_return_to(return_to: str | None) -> str:
    """Validate the post-login redirect target to prevent open-redirects.

    Only same-app absolute URLs (under ``NEXT_PUBLIC_APP_URL``) or root-relative
    paths are honoured; anything else falls back to the app root.
    """
    app_url = settings.NEXT_PUBLIC_APP_URL.rstrip("/")
    if not return_to:
        return app_url or "/"
    if return_to.startswith("/") and not return_to.startswith("//") and not return_to.startswith("/\\"):
        return f"{app_url}{return_to}" if app_url else return_to
    # Absolute URLs must be the app origin EXACTLY or a path under it —
    # a bare prefix match would let "https://app.example.com.evil.com" through.
    if app_url and (return_to == app_url or return_to.startswith(app_url + "/")):
        return return_to
    return app_url or "/"


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class AuthConfig(BaseModel):
    """Non-secret auth capability advertisement for the frontend."""

    configured: bool
    login_path: str = "/auth/login"
    logout_path: str = "/auth/logout"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/login", name="auth_login")
def login(
    request: Request,
    return_to: str | None = Query(
        default=None, description="Path/URL within the app to land on after login."
    ),
    organization_id: str | None = Query(
        default=None, description="Force a specific WorkOS organization's SSO connection."
    ),
    connection_id: str | None = Query(
        default=None, description="Force a specific SSO connection (SAML/OIDC)."
    ),
    provider: str | None = Query(
        default=None, description="Force a provider, e.g. 'GoogleOAuth'. Default: hosted AuthKit."
    ),
    login_hint: str | None = Query(default=None, description="Pre-fill the email field."),
) -> RedirectResponse:
    """Begin login: redirect to WorkOS hosted AuthKit (or a forced connection).

    Returns ``503`` when WorkOS is not configured on the server.
    """
    try:
        state = auth_service.sign_state(return_to)
        url = auth_service.authorization_url(
            redirect_uri=_callback_url(request),
            state=state,
            provider=provider or "authkit",
            connection_id=connection_id,
            organization_id=organization_id,
            login_hint=login_hint,
        )
    except auth_service.WorkOSError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    response = RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    # Bind the state to this browser (defence-in-depth CSRF check at callback).
    response.set_cookie(
        key=auth_service.STATE_COOKIE_NAME,
        value=state,
        httponly=True,
        secure=settings.ENV == "production",
        samesite="lax",
        max_age=600,
        path="/auth",
    )
    return response


@router.get("/callback", name="auth_callback")
def callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> RedirectResponse:
    """Handle the WorkOS redirect: verify state, exchange code, set session cookie."""
    if error:
        logger.warning("WorkOS returned an error at callback: %s (%s)", error, error_description)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Login failed: {error_description or error}",
        )
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing authorization code.")

    # CSRF: the state must verify AND match the cookie we set at /login.
    # The cookie is REQUIRED — accepting a signed state without the browser
    # cookie would allow login-CSRF (an attacker forwarding their own callback
    # URL to a victim whose browser never initiated /auth/login).
    state_payload = auth_service.verify_state(state)
    cookie_state = request.cookies.get(auth_service.STATE_COOKIE_NAME)
    if state_payload is None or not cookie_state or cookie_state != state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired login state.")

    try:
        result = auth_service.complete_login(code=code, redirect_uri=_callback_url(request))
    except auth_service.WorkOSError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return_to = _safe_return_to(state_payload.get("return_to"))
    response = RedirectResponse(return_to, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    response.set_cookie(value=auth_service.seal_session(result.session_payload), **auth_service.cookie_params())
    response.delete_cookie(auth_service.STATE_COOKIE_NAME, path="/auth")
    return response


@router.post("/refresh")
def refresh(request: Request) -> JSONResponse:
    """Renew the session from the stored WorkOS refresh token.

    Reads the current (possibly near-expiry) signed cookie, exchanges its refresh
    token for a new session, and re-sets the cookie. ``401`` if there is no usable
    session to refresh.
    """
    payload = auth_service.unseal_session(request.cookies.get(auth_service.SESSION_COOKIE_NAME))
    refresh_token = payload.get("rt") if payload else None
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No session to refresh.")

    try:
        result = auth_service.refresh_login(refresh_token=refresh_token)
    except auth_service.WorkOSError as exc:
        # A rejected refresh token means the session is truly over -> clear it.
        response = JSONResponse({"detail": str(exc)}, status_code=status.HTTP_401_UNAUTHORIZED)
        response.delete_cookie(auth_service.SESSION_COOKIE_NAME, path="/")
        return response

    response = JSONResponse(result.user.model_dump())
    response.set_cookie(value=auth_service.seal_session(result.session_payload), **auth_service.cookie_params())
    return response


@router.post("/logout")
@router.get("/logout")
def logout(
    request: Request,
    return_to: str | None = Query(default=None, description="Where to land after logout."),
) -> RedirectResponse:
    """Clear the local session and redirect through the WorkOS hosted logout."""
    payload = auth_service.unseal_session(request.cookies.get(auth_service.SESSION_COOKIE_NAME))
    session_id = payload.get("sid") if payload else None
    target = _safe_return_to(return_to)
    url = auth_service.logout_url(session_id=session_id, return_to=target)

    response = RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    response.delete_cookie(auth_service.SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/me", response_model=CurrentUser)
def me(user: CurrentUserDep) -> CurrentUser:
    """Return the authenticated principal (role drives the frontend's UI gating)."""
    return user


@router.get("/config", response_model=AuthConfig)
def config() -> AuthConfig:
    """Advertise whether WorkOS auth is wired — no secrets leaked."""
    return AuthConfig(configured=settings.auth_configured)
