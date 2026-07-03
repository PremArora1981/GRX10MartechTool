"""Settings router — validation profiles, web-search toggle, audience switcher
(spec §5 screen 9, Q5, Q8, Q10).

Endpoints
---------
Validation profiles (Q5):
  GET  /settings/profiles                   — list all profiles, active first
  GET  /settings/profiles/active            — the single active profile
  PUT  /settings/profiles/{id}/activate     — atomically flip ``is_active``
  POST /settings/profiles/clone             — clone + optional per-threshold tweaks

Web-search fallback (Q8):
  GET  /settings/web-search                 — current on/off state
  PUT  /settings/web-search                 — toggle (admin only)

Audience switcher (Q10):
  GET  /settings/audience                   — current default audience value
  PUT  /settings/audience                   — set audience (admin only)

Design notes
------------
* **Profile activation is atomic**: a two-step UPDATE (clear all → set one) runs
  inside the request-scoped session transaction so ``cell_triangulation_summary``
  never reads a moment with zero or two active profiles.

* **Web-search toggle** persists in an ``_engagement_settings`` commentary row
  (JSON payload in ``body_markdown``). It also syncs ``sources.enabled`` for every
  source with ``access_method='web_search'`` so the pipeline's source-enabled
  guard respects the toggle without reading the settings row at runtime.
  The method-level hard-cap (Class C, LOW confidence max) is enforced by the
  ``method_registry`` row for ``web_search_extraction`` — not here.

* **Audience switcher** is stored in the same commentary settings carrier.
  The ``commentary`` table's own ``audience`` column controls *content* filtering;
  this setting controls the *default filter* the UI pre-selects.

* Neither the web-search toggle nor the audience switcher requires a new table.
  The ``commentary`` table's ``scope_type`` column is a free-form discriminator;
  using ``_engagement_settings`` as a sentinel is intentional and documented here.
"""

from __future__ import annotations

import datetime
import json
import logging
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from backend.app.deps import CurrentUserDep, DbSession, EngagementDep, require_admin
from backend.app.models import Commentary, Source, ValidationProfile
from backend.app.schemas import CurrentUser

logger = logging.getLogger("grx10.routers.settings")

router = APIRouter(prefix="/settings", tags=["settings"])

# Commentary scope_type used as the engagement-level settings carrier.
_SETTINGS_SCOPE: str = "_engagement_settings"

_VALID_AUDIENCES: frozenset[str] = frozenset({"all", "analyst", "business", "external"})

_VALID_INDEPENDENCE_LEVELS: frozenset[str] = frozenset(
    {"method", "method_x_source_class"}
)


# ---------------------------------------------------------------------------
# Validation profile schemas
# ---------------------------------------------------------------------------


class ValidationProfileOut(BaseModel):
    """Full representation of a ``validation_profiles`` row."""

    model_config = ConfigDict(from_attributes=True)

    profile_id: int
    name: str
    is_active: bool
    independence_level: str
    high_min_distinct_methods: int
    high_max_spread: Decimal
    high_require_tier_a: bool
    high_min_source_classes: int
    medium_min_distinct_methods: int
    medium_max_spread: Decimal
    medium_alt_min_methods: int | None = None
    medium_alt_max_spread: Decimal | None = None


class ValidationProfileCloneIn(BaseModel):
    """Request body for ``POST /settings/profiles/clone``.

    Every threshold field is optional; omitting it inherits the value from the
    source profile.  This is the "clone-and-tweak" escape hatch (Q5).
    """

    source_profile_id: int = Field(description="Profile to copy thresholds from.")
    name: str = Field(
        min_length=1,
        max_length=200,
        description="Unique name for the new profile.",
    )
    # Optional per-threshold overrides.
    independence_level: str | None = Field(
        default=None,
        description="method | method_x_source_class",
    )
    high_min_distinct_methods: int | None = Field(default=None, ge=1)
    high_max_spread: Decimal | None = Field(
        default=None, ge=Decimal("0"), le=Decimal("1")
    )
    high_require_tier_a: bool | None = None
    high_min_source_classes: int | None = Field(default=None, ge=1)
    medium_min_distinct_methods: int | None = Field(default=None, ge=1)
    medium_max_spread: Decimal | None = Field(
        default=None, ge=Decimal("0"), le=Decimal("1")
    )
    medium_alt_min_methods: int | None = Field(default=None, ge=1)
    medium_alt_max_spread: Decimal | None = Field(
        default=None, ge=Decimal("0"), le=Decimal("1")
    )


# ---------------------------------------------------------------------------
# Web-search schemas
# ---------------------------------------------------------------------------


class WebSearchState(BaseModel):
    """Current state of the web-search fallback (Q8)."""

    enabled: bool = Field(
        description=(
            "Whether the web-search fallback is active for this engagement. "
            "On by default; always Class C / LOW confidence cap."
        )
    )
    web_search_source_count: int = Field(
        description=(
            "Number of sources with ``access_method='web_search'`` currently in "
            "the sources table. Their ``enabled`` flag is kept in sync with this toggle."
        )
    )


class WebSearchToggleIn(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# Audience schemas
# ---------------------------------------------------------------------------


class AudienceState(BaseModel):
    """Current default audience value for content filtering (Q10)."""

    audience: str = Field(
        description=(
            "Default audience filter: ``all | analyst | business | external``. "
            "Controls which commentary rows are surfaced by default."
        )
    )


class AudienceSetIn(BaseModel):
    audience: str = Field(
        description="New default audience: all | analyst | business | external"
    )


# ---------------------------------------------------------------------------
# Engagement-settings store (commentary table as a key-value carrier)
# ---------------------------------------------------------------------------


def _load_engagement_settings(db: Session, engagement_id: str) -> dict:
    """Read the engagement-settings JSON from the commentary carrier row.

    Returns an empty dict when no settings row exists yet; callers apply
    their own defaults.  Scoped to the active engagement.
    """
    row: Commentary | None = db.scalar(
        select(Commentary)
        .where(
            Commentary.scope_type == _SETTINGS_SCOPE,
            Commentary.engagement_id == engagement_id,
        )
        .order_by(Commentary.created_at.desc())
        .limit(1)
    )
    if row is None:
        return {}
    try:
        return json.loads(row.body_markdown)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "engagement-settings commentary row has non-JSON body_markdown; ignoring"
        )
        return {}


def _save_engagement_settings(
    db: Session, settings: dict, actor: str, engagement_id: str
) -> None:
    """Upsert the engagement-settings commentary carrier row.

    The ``commentary`` table has no unique constraint on ``(scope_type, scope_id)``
    so we rely on SELECT-then-UPDATE semantics within the same transaction, updating
    the most-recently created row when one already exists.

    Why commentary?  There is no dedicated engagement-settings table in v1.  Using
    an ``_engagement_settings`` sentinel in the ``scope_type`` discriminator is the
    least-invasive approach that requires no schema migration and is fully auditable
    (the row carries ``author`` + ``created_at``).
    """
    existing: Commentary | None = db.scalar(
        select(Commentary)
        .where(
            Commentary.scope_type == _SETTINGS_SCOPE,
            Commentary.engagement_id == engagement_id,
        )
        .order_by(Commentary.created_at.desc())
        .limit(1)
    )
    body = json.dumps(settings, default=str, sort_keys=True)
    if existing is not None:
        existing.body_markdown = body
        existing.author = actor
    else:
        db.add(
            Commentary(
                engagement_id=engagement_id,
                scope_type=_SETTINGS_SCOPE,
                scope_id=None,
                body_markdown=body,
                audience="all",
                author=actor,
                created_at=datetime.datetime.now(datetime.timezone.utc),
            )
        )


def _count_web_search_sources(db: Session, engagement_id: str) -> int:
    """Count sources registered with ``access_method='web_search'`` (engagement-scoped)."""
    return db.scalar(
        select(func.count())
        .select_from(Source)
        .where(
            Source.access_method == "web_search",
            Source.engagement_id == engagement_id,
        )
    ) or 0


# ---------------------------------------------------------------------------
# Validation profile routes
# ---------------------------------------------------------------------------


@router.get(
    "/profiles",
    response_model=list[ValidationProfileOut],
    summary="List all validation profiles",
)
def list_profiles(
    db: DbSession,
    _user: CurrentUserDep,
) -> list[ValidationProfileOut]:
    """Return all validation profiles, active profile first.

    The seeded profiles (Light / Standard / Conservative / Audit-grade) are always
    present; cloned profiles appear after them ordered by ``profile_id``.
    """
    rows = db.execute(
        select(ValidationProfile).order_by(
            ValidationProfile.is_active.desc(),
            ValidationProfile.profile_id,
        )
    ).scalars().all()
    return [ValidationProfileOut.model_validate(r) for r in rows]


@router.get(
    "/profiles/active",
    response_model=ValidationProfileOut,
    summary="Get the active validation profile",
)
def get_active_profile(
    db: DbSession,
    engagement_id: EngagementDep,
    _user: CurrentUserDep,
) -> ValidationProfileOut:
    """Return the validation profile active for this engagement.

    The active profile name lives on the engagement row (``engagements.active_profile``);
    the full threshold set is looked up from the global ``validation_profiles`` table
    by name.  Raises 404 if the engagement has no active profile set or the named
    profile no longer exists.
    The ``cell_triangulation_summary`` view reads this profile; if it is absent,
    confidence cannot be computed.
    """
    active_name: str | None = db.execute(
        text("SELECT active_profile FROM engagements WHERE engagement_id = :eng"),
        {"eng": engagement_id},
    ).scalar_one_or_none()

    row: ValidationProfile | None = None
    if active_name is not None:
        row = db.scalar(
            select(ValidationProfile)
            .where(ValidationProfile.name == active_name)
            .limit(1)
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No active validation profile found. "
                "Run the config loader (services/config_loader.py) to seed profiles."
            ),
        )
    return ValidationProfileOut.model_validate(row)


@router.put(
    "/profiles/{profile_id}/activate",
    response_model=ValidationProfileOut,
    summary="Atomically activate a validation profile",
)
def activate_profile(
    profile_id: int,
    db: DbSession,
    engagement_id: EngagementDep,
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> ValidationProfileOut:
    """Set this engagement's active validation profile by name.

    The active profile is now per-engagement (``engagements.active_profile``),
    not a global ``validation_profiles.is_active`` flag.  We resolve the target
    profile by ``profile_id`` (validating it exists in the global registry), then
    record its ``name`` on the engagement row.  This runs inside the request-scoped
    session transaction, which commits on a clean response return.

    Because ``cell_triangulation_summary`` is a materialized view, callers
    should trigger ``REFRESH MATERIALIZED VIEW cell_triangulation_summary``
    (via the pipeline or a dedicated admin endpoint) after switching profiles
    to propagate the new thresholds to confidence verdicts.

    Requires ``owner`` or ``admin`` role.
    """
    target: ValidationProfile | None = db.scalar(
        select(ValidationProfile).where(ValidationProfile.profile_id == profile_id)
    )
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Validation profile {profile_id} not found.",
        )

    # Record the active profile name on the engagement row (per-engagement setting).
    db.execute(
        text(
            "UPDATE engagements SET active_profile = :name WHERE engagement_id = :eng"
        ),
        {"name": target.name, "eng": engagement_id},
    )
    db.flush()  # materialise within the transaction before the response body builds

    logger.info(
        "user=%s activated validation_profile profile_id=%d name='%s' "
        "for engagement=%s",
        user.id,
        profile_id,
        target.name,
        engagement_id,
    )
    return ValidationProfileOut.model_validate(target)


@router.post(
    "/profiles/clone",
    response_model=ValidationProfileOut,
    status_code=status.HTTP_201_CREATED,
    summary="Clone + optionally tweak a validation profile",
)
def clone_profile(
    body: ValidationProfileCloneIn,
    db: DbSession,
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> ValidationProfileOut:
    """Clone an existing profile, optionally overriding specific thresholds.

    The new profile is **inactive** by default.  Call
    ``PUT /settings/profiles/{id}/activate`` to switch to it.

    This is the "clone-and-tweak" escape hatch (Q5): analysts can tighten or
    loosen individual thresholds without losing the standard presets.

    Requires ``owner`` or ``admin`` role.
    """
    # Guard: name must be unique.
    existing_name: ValidationProfile | None = db.scalar(
        select(ValidationProfile).where(ValidationProfile.name == body.name)
    )
    if existing_name is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A profile named '{body.name}' already exists (id={existing_name.profile_id}).",
        )

    source: ValidationProfile | None = db.scalar(
        select(ValidationProfile).where(
            ValidationProfile.profile_id == body.source_profile_id
        )
    )
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source profile {body.source_profile_id} not found.",
        )

    # Validate independence_level if provided.
    if (
        body.independence_level is not None
        and body.independence_level not in _VALID_INDEPENDENCE_LEVELS
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid independence_level '{body.independence_level}'. "
                f"Must be one of: {sorted(_VALID_INDEPENDENCE_LEVELS)}."
            ),
        )

    # Helper: pick override or inherit from source.
    def _pick(override, fallback):
        return override if override is not None else fallback

    new_profile = ValidationProfile(
        name=body.name,
        is_active=False,
        independence_level=_pick(body.independence_level, source.independence_level),
        high_min_distinct_methods=_pick(
            body.high_min_distinct_methods, source.high_min_distinct_methods
        ),
        high_max_spread=_pick(body.high_max_spread, source.high_max_spread),
        high_require_tier_a=_pick(body.high_require_tier_a, source.high_require_tier_a),
        high_min_source_classes=_pick(
            body.high_min_source_classes, source.high_min_source_classes
        ),
        medium_min_distinct_methods=_pick(
            body.medium_min_distinct_methods, source.medium_min_distinct_methods
        ),
        medium_max_spread=_pick(body.medium_max_spread, source.medium_max_spread),
        medium_alt_min_methods=_pick(
            body.medium_alt_min_methods, source.medium_alt_min_methods
        ),
        medium_alt_max_spread=_pick(
            body.medium_alt_max_spread, source.medium_alt_max_spread
        ),
    )
    db.add(new_profile)
    db.flush()  # assign profile_id before returning

    logger.info(
        "user=%s cloned profile_id=%d ('%s') -> new profile_id=%d name='%s'",
        user.id,
        body.source_profile_id,
        source.name,
        new_profile.profile_id,
        body.name,
    )
    return ValidationProfileOut.model_validate(new_profile)


# ---------------------------------------------------------------------------
# Web-search fallback routes
# ---------------------------------------------------------------------------


@router.get(
    "/web-search",
    response_model=WebSearchState,
    summary="Get web-search fallback state",
)
def get_web_search_state(
    db: DbSession,
    engagement_id: EngagementDep,
    _user: CurrentUserDep,
) -> WebSearchState:
    """Return whether the web-search fallback is enabled for this engagement (Q8).

    Default is ``enabled=True`` (on by default per the spec).  The toggle now lives
    on the engagement row (``engagements.web_search_enabled``); this endpoint reads it.
    """
    enabled_raw = db.execute(
        text("SELECT web_search_enabled FROM engagements WHERE engagement_id = :eng"),
        {"eng": engagement_id},
    ).scalar_one_or_none()
    enabled: bool = True if enabled_raw is None else bool(enabled_raw)
    return WebSearchState(
        enabled=enabled,
        web_search_source_count=_count_web_search_sources(db, engagement_id),
    )


@router.put(
    "/web-search",
    response_model=WebSearchState,
    summary="Toggle web-search fallback on/off",
)
def set_web_search_state(
    body: WebSearchToggleIn,
    db: DbSession,
    engagement_id: EngagementDep,
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> WebSearchState:
    """Enable or disable the web-search fallback for this engagement (Q8).

    Two effects:

    1. **Persists** the toggle on the engagement row
       (``engagements.web_search_enabled``) so the pipeline and status page read
       the same value.

    2. **Syncs** ``sources.enabled`` for every source with
       ``access_method='web_search'`` so the pipeline's pre-flight
       source-enabled guard immediately respects the toggle without needing
       to read the settings row at run-time.

    The method-level hard-cap (Class C, ``confidence_cap='low'``) is enforced
    by the ``method_registry`` row for ``web_search_extraction``; it is never
    overridden here.

    Requires ``owner`` or ``admin`` role.
    """
    db.execute(
        text(
            "UPDATE engagements SET web_search_enabled = :val "
            "WHERE engagement_id = :eng"
        ),
        {"val": body.enabled, "eng": engagement_id},
    )

    # Sync the enabled flag on all web_search sources for this engagement.
    db.execute(
        update(Source)
        .where(
            Source.access_method == "web_search",
            Source.engagement_id == engagement_id,
        )
        .values(enabled=body.enabled)
    )
    db.flush()

    ws_count = _count_web_search_sources(db, engagement_id)
    logger.info(
        "user=%s set web_search_enabled=%s (synced %d sources)",
        user.id,
        body.enabled,
        ws_count,
    )
    return WebSearchState(enabled=body.enabled, web_search_source_count=ws_count)


# ---------------------------------------------------------------------------
# Audience switcher routes
# ---------------------------------------------------------------------------


@router.get(
    "/audience",
    response_model=AudienceState,
    summary="Get current default audience",
)
def get_audience(
    db: DbSession,
    engagement_id: EngagementDep,
    _user: CurrentUserDep,
) -> AudienceState:
    """Return the current default audience for content filtering (Q10).

    The audience value controls which ``commentary`` rows are surfaced by default
    in the Cell Detail, Players, and Assumptions screens.  Any authenticated user
    can read it; setting it requires admin.
    """
    cfg = _load_engagement_settings(db, engagement_id)
    return AudienceState(audience=cfg.get("audience", "all"))


@router.put(
    "/audience",
    response_model=AudienceState,
    summary="Set default audience",
)
def set_audience(
    body: AudienceSetIn,
    db: DbSession,
    engagement_id: EngagementDep,
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> AudienceState:
    """Set the default audience for content filtering (admin only, Q10).

    Valid values: ``all | analyst | business | external``.

    This is the audience *switcher* described in spec §5 screen 9.  It controls
    which commentary rows are pre-selected in the UI.  Individual commentary rows
    still carry their own ``audience`` column; this setting is the engagement-level
    default filter that the UI applies when the user has not overridden it.

    Requires ``owner`` or ``admin`` role.
    """
    if body.audience not in _VALID_AUDIENCES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid audience '{body.audience}'. "
                f"Must be one of: {sorted(_VALID_AUDIENCES)}."
            ),
        )

    cfg = _load_engagement_settings(db, engagement_id)
    cfg["audience"] = body.audience
    _save_engagement_settings(db, cfg, actor=user.id, engagement_id=engagement_id)
    db.flush()

    logger.info("user=%s set default audience='%s'", user.id, body.audience)
    return AudienceState(audience=body.audience)
