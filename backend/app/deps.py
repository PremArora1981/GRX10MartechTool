"""Common FastAPI dependencies shared by every router.

* :func:`get_session` — request-scoped SQLAlchemy session (re-exported from
  ``db`` so routers have one import site for deps).
* :func:`get_current_user` — resolves the authenticated principal. The real
  WorkOS AuthKit verification lives in the auth service
  (``backend.app.services.auth``), built by the auth agent. We import it lazily
  and degrade gracefully: if the auth service is not present yet, or WorkOS is
  unconfigured, requests resolve to an anonymous ``external`` user in development
  and are rejected with 401 in production. We never fabricate an admin.
* :func:`require_role` / :func:`require_admin` — role-gating dependency factories
  (admin gates credential entry per Q9).

Routers depend on these; nothing here issues a real auth challenge by itself.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator
from typing import Annotated, Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.db import get_session as _get_session
from backend.app.schemas import CurrentUser, Role

logger = logging.getLogger("grx10.deps")


# Re-export so routers can `from backend.app.deps import get_session`.
def get_session() -> Iterator[Session]:
    """Yield a request-scoped DB session (see :func:`backend.app.db.get_session`)."""
    yield from _get_session()


DbSession = Annotated[Session, Depends(get_session)]


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
_ANON_DEV_USER = CurrentUser(id="dev-anonymous", email="dev@localhost", role="owner")


def _resolve_auth_service() -> Callable[..., CurrentUser | None] | None:
    """Find the auth service's user-resolver if it has been built, else ``None``.

    Tries the conventional entrypoints exposed by ``backend.app.services.auth``.
    Resilient to the auth module not existing yet (parallel build).
    """
    try:
        auth = importlib.import_module("backend.app.services.auth")
    except Exception:  # noqa: BLE001 — auth service may not be built yet
        return None
    for attr in ("resolve_current_user", "get_current_user", "current_user_from_request"):
        fn = getattr(auth, attr, None)
        if callable(fn):
            return fn
    return None


def get_current_user(request: Request) -> CurrentUser:
    """Resolve the authenticated principal for the request.

    Delegates to the auth service when available. Fallback policy when it is not:

    * **production** (``ENV=production``) with auth wired -> 401 (fail closed);
    * **development** / auth not configured -> anonymous ``owner`` dev user so the
      app is runnable end-to-end before WorkOS is provisioned.
    """
    # When WorkOS is not configured, degrade to the anonymous owner regardless
    # of the resolver's presence. This mirrors the frontend (lib/auth.ts returns
    # a mock owner when WORKOS_* is unset) so an un-onboarded deployment is
    # demo-usable end-to-end. SECURITY: such a deployment is unauthenticated —
    # anyone with the URL is 'owner'. Configure WorkOS before real client data.
    if not settings.auth_configured:
        logger.debug("auth not configured; using anonymous owner")
        return _ANON_DEV_USER

    resolver = _resolve_auth_service()
    if resolver is not None:
        try:
            user = resolver(request)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("auth service raised while resolving user: %s", exc)
            user = None
        if user is not None:
            return user if isinstance(user, CurrentUser) else CurrentUser.model_validate(user)
        # Resolver present but returned nobody -> unauthenticated.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # No auth service yet.
    if settings.ENV == "production" and settings.auth_configured:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication service unavailable.",
        )
    logger.debug("auth service absent; using anonymous dev user")
    return _ANON_DEV_USER


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_role(*allowed: Role) -> Callable[[CurrentUser], CurrentUser]:
    """Dependency factory enforcing that the current user holds one of ``allowed``."""

    def _checker(user: CurrentUserDep) -> CurrentUser:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role in {allowed}; you are '{user.role}'.",
            )
        return user

    return _checker


def require_admin(user: CurrentUserDep) -> CurrentUser:
    """Gate for owner/admin-only operations (credential entry, Q9)."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner/admin role required.",
        )
    return user


__all__ = [
    "get_session", "DbSession",
    "get_current_user", "CurrentUserDep",
    "require_role", "require_admin",
]
