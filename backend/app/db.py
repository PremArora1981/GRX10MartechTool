"""SQLAlchemy 2.0 database layer — engine, sessionmaker, and the FastAPI session
dependency that every backend router imports.

Sync engine on the psycopg3 driver (the stack decision: SQLAlchemy 2.0 sync, not
async). One process-wide :class:`Engine` with ``pool_pre_ping`` so a Render
Postgres connection dropped overnight is transparently recycled.

Usage in a router::

    from fastapi import Depends
    from sqlalchemy.orm import Session
    from backend.app.db import get_session

    @router.get(...)
    def handler(db: Session = Depends(get_session)):
        ...
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import settings

logger = logging.getLogger("grx10.db")


def _build_engine() -> Engine:
    """Construct the process-wide engine from settings (psycopg3, pooled)."""
    return create_engine(
        settings.sqlalchemy_database_url,
        future=True,
        pool_pre_ping=True,         # recycle stale connections (Render idle drops)
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        echo=settings.DB_ECHO,
    )


# Process-wide engine + session factory. Importing modules share these.
engine: Engine = _build_engine()

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
    class_=Session,
)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped :class:`Session`.

    Commits on a clean exit, rolls back on any exception, and always closes the
    session (returning the connection to the pool). Read-only handlers are
    unaffected by the commit (nothing dirty to flush).
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping() -> bool:
    """Cheap connectivity check used by the ``/health`` endpoint.

    Returns ``True`` if a trivial ``SELECT 1`` round-trips, ``False`` otherwise
    (never raises — health checks must not 500 on a transient DB blip).
    """
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 — health probe must degrade, not raise
        logger.warning("database ping failed: %s", exc)
        return False
