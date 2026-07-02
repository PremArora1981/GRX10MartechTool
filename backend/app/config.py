"""Application settings (Pydantic v2 ``BaseSettings``).

Single, importable settings object for the whole backend. Values come from the
environment (Render dashboard / ``grx10-shared`` env-var group in prod; a local
``.env`` in dev — see ``.env.example``). Nothing here has a production-safe
default for a secret: secrets are read from the environment and the app degrades
gracefully (features that need a missing secret announce themselves) rather than
shipping a fake value.

Import once::

    from backend.app.config import settings
    engine = create_engine(settings.sqlalchemy_database_url)
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_db_url(url: str) -> str:
    """Coerce a Render/Heroku ``postgres(ql)://`` URL to the psycopg3 driver form.

    Render hands out ``postgres://…`` connection strings; SQLAlchemy 2.0 needs an
    explicit driver (``postgresql+psycopg://``) to select psycopg3 over the legacy
    psycopg2. Already-qualified URLs (``postgresql+psycopg://``) pass through
    unchanged so callers can paste either form.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Settings(BaseSettings):
    """Typed view of the process environment.

    ``extra='ignore'`` so the many connector-specific and frontend-only env vars
    that share the same environment (NEXT_PUBLIC_*, per-connector API keys) do not
    cause a validation error here.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Database (psycopg3, sync) ---
    DATABASE_URL: str = Field(
        default="postgresql+psycopg://grx10:grx10@localhost:5432/grx10_market_research",
        description="Postgres connection string; postgres:// is auto-upgraded to the psycopg3 driver.",
    )
    DB_POOL_SIZE: int = Field(default=5, description="SQLAlchemy connection-pool size.")
    DB_MAX_OVERFLOW: int = Field(default=10, description="Extra connections beyond the pool size.")
    DB_ECHO: bool = Field(default=False, description="Echo SQL to the logger (dev only).")

    # --- WorkOS managed auth (Q10) ---
    WORKOS_API_KEY: str | None = Field(default=None)
    WORKOS_CLIENT_ID: str | None = Field(default=None)
    WORKOS_COOKIE_PASSWORD: str | None = Field(
        default=None, description=">= 32 chars; encrypts the AuthKit session cookie."
    )
    WORKOS_REDIRECT_URI: str | None = Field(default=None)

    # --- Credential envelope-encryption master key (Q9) ---
    CRED_MASTER_KEY: str | None = Field(
        default=None,
        description="Wraps per-credential data keys (pgcrypto). Rotating it invalidates all stored secrets.",
    )

    # --- Anthropic (Q6 AI field-mapping, Q8 web-search fallback) ---
    ANTHROPIC_API_KEY: str | None = Field(default=None)

    # --- Pipeline failure notifications (Q7) ---
    SLACK_WEBHOOK_URL: str | None = Field(default=None)

    # --- Service URLs / CORS ---
    NEXT_PUBLIC_APP_URL: str = Field(
        default="http://localhost:3000",
        description="Public URL of the Next.js frontend; primary allowed CORS origin.",
    )
    EXTRA_CORS_ORIGINS: str = Field(
        default="",
        description="Comma-separated additional allowed origins (e.g. preview deploys).",
    )

    # --- Misc ---
    LOG_LEVEL: str = Field(default="INFO")
    PIPELINE_SINCE: str | None = Field(default=None)
    ENV: str = Field(default="development", description="development | production")

    # ------------------------------------------------------------------ #
    # Derived values
    # ------------------------------------------------------------------ #
    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_database_url(self) -> str:
        """``DATABASE_URL`` normalised to the SQLAlchemy + psycopg3 driver form."""
        return normalize_db_url(self.DATABASE_URL)

    @property
    def cors_origins(self) -> list[str]:
        """Allowed browser origins: the frontend URL plus any extras + localhost dev."""
        origins = {self.NEXT_PUBLIC_APP_URL.rstrip("/")}
        for extra in self.EXTRA_CORS_ORIGINS.split(","):
            extra = extra.strip().rstrip("/")
            if extra:
                origins.add(extra)
        # Always allow common local dev origins so the app is runnable out of the box.
        origins.update({"http://localhost:3000", "http://127.0.0.1:3000"})
        return sorted(origins)

    @property
    def auth_configured(self) -> bool:
        """True when WorkOS is fully wired (gate auth-dependent features cleanly)."""
        return bool(self.WORKOS_API_KEY and self.WORKOS_CLIENT_ID and self.WORKOS_COOKIE_PASSWORD)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor (one parse of the environment per process)."""
    return Settings()


# Module-level singleton for ergonomic imports.
settings: Settings = get_settings()
