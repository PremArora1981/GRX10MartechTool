"""Commentary router — CRUD on the ``commentary`` table, audience-scoped (Layer 4).

The ``commentary`` table carries analyst notes, executive summaries, and
business-visible explanations attached to any scope level: individual cells,
subcategories, taxonomy families, or the entire engagement.  Each row carries
an ``audience`` tag (``all | analyst | business | external``) that controls
which user roles may read it.

Access model (Q10)
------------------
Read access is role-gated via :mod:`backend.app.services.audience`:

* **owner / admin** — sees all commentary regardless of audience tag.
* **analyst**        — sees ``all`` + ``analyst`` tagged rows.
* **business**       — sees ``all`` + ``business`` tagged rows.
* **external**       — sees ``all`` + ``external`` tagged rows.

Write access:

* **Create / Update** — requires ``analyst``, ``admin``, or ``owner`` role.
* **Delete**          — requires ``admin`` or ``owner`` role.

Scope types
-----------
The ``scope_type`` column discriminates the object level the commentary
annotates.  Valid values are ``cell | subcategory | family | engagement``.
The internal ``_engagement_settings`` sentinel used by the settings router is
treated as a private namespace and is never exposed here.

Routes (auto-discovered by ``main.py`` router discovery)
---------------------------------------------------------
GET    /api/commentary                     — paginated list, audience-filtered
POST   /api/commentary                     — create a new commentary row
GET    /api/commentary/{commentary_id}     — single row, audience-gated
PUT    /api/commentary/{commentary_id}     — update body_markdown and/or audience
DELETE /api/commentary/{commentary_id}     — hard delete (admin / owner only)
"""

from __future__ import annotations

import datetime
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.deps import CurrentUserDep, DbSession, require_admin, require_role
from backend.app.models import Commentary
from backend.app.schemas import CommentaryOut, CurrentUser
from backend.app.services.audience import (
    VALID_AUDIENCE_TAGS,
    is_commentary_visible,
    visible_audience_tags,
)

logger = logging.getLogger("grx10.routers.commentary")

router = APIRouter(prefix="/commentary", tags=["commentary"])

# The settings router stores engagement-level settings in the commentary table
# using this sentinel value for scope_type.  We never expose it through the
# public CRUD endpoints.
_SETTINGS_SCOPE: str = "_engagement_settings"

# Valid scope_type values accepted by the public CRUD API.
_VALID_SCOPE_TYPES: frozenset[str] = frozenset(
    {"cell", "subcategory", "family", "engagement"}
)


# ---------------------------------------------------------------------------
# Request / response schemas (local; CommentaryOut lives in schemas.py)
# ---------------------------------------------------------------------------


class CommentaryCreate(BaseModel):
    """Request body for ``POST /api/commentary``."""

    scope_type: str = Field(
        description=(
            "Scope level for this commentary: "
            "``cell | subcategory | family | engagement``."
        )
    )
    scope_id: int | None = Field(
        default=None,
        description=(
            "Primary key of the scoped object (cell_id, subcategory_id, etc.). "
            "Omit for engagement-wide commentary (``scope_type='engagement'``)."
        ),
    )
    body_markdown: str = Field(
        min_length=1,
        description="Commentary body in Markdown.  Must be non-empty.",
    )
    audience: str = Field(
        default="all",
        description=(
            "Audience visibility tag: ``all | analyst | business | external``. "
            "Defaults to ``all`` (visible to every authenticated user). "
            "Only ``owner``/``admin`` may set ``external``."
        ),
    )


class CommentaryUpdate(BaseModel):
    """Request body for ``PUT /api/commentary/{commentary_id}``.

    Both fields are optional; supply at least one.  Omitting a field leaves the
    current value unchanged (no implicit null-out).
    """

    body_markdown: str | None = Field(
        default=None,
        min_length=1,
        description="Updated Markdown body.  Omit to keep the current value.",
    )
    audience: str | None = Field(
        default=None,
        description=(
            "Updated audience tag: ``all | analyst | business | external``. "
            "Omit to keep the current value.  "
            "Only ``owner``/``admin`` may set ``external``."
        ),
    )


class CommentaryList(BaseModel):
    """Paginated commentary list response."""

    items: list[CommentaryOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_commentary(commentary_id: int, session: Session) -> Commentary:
    """Return the ``Commentary`` ORM row or raise HTTP 404.

    Does **not** apply audience filtering — callers must check visibility
    separately so that restricted rows return the same 404 as missing rows
    (no information disclosure).
    """
    row = session.get(Commentary, commentary_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Commentary {commentary_id} not found.",
        )
    return row


def _validate_scope_type(scope_type: str) -> None:
    """Raise HTTP 422 if ``scope_type`` is not a public-API-legal value.

    The internal ``_engagement_settings`` sentinel and any unknown values are
    rejected here to prevent data from leaking into or out of the settings
    namespace through the commentary CRUD endpoints.
    """
    if scope_type not in _VALID_SCOPE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid scope_type '{scope_type}'. "
                f"Must be one of: {sorted(_VALID_SCOPE_TYPES)}."
            ),
        )


def _validate_audience(audience: str) -> None:
    """Raise HTTP 422 if ``audience`` is not a valid tag value."""
    if audience not in VALID_AUDIENCE_TAGS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid audience '{audience}'. "
                f"Must be one of: {sorted(VALID_AUDIENCE_TAGS)}."
            ),
        )


def _check_external_permission(audience: str, user: CurrentUser) -> None:
    """Raise HTTP 403 if setting the ``external`` audience without admin/owner."""
    if audience == "external" and not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Creating or updating commentary with audience='external' "
                "requires owner or admin role."
            ),
        )


# ---------------------------------------------------------------------------
# GET /api/commentary — paginated list
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=CommentaryList,
    summary="List commentary — paginated, audience-filtered",
)
def list_commentary(
    db: DbSession,
    user: CurrentUserDep,
    scope_type: Annotated[
        str | None,
        Query(
            description=(
                "Narrow by scope level: ``cell | subcategory | family | engagement``. "
                "Omit to return rows from all scope levels."
            )
        ),
    ] = None,
    scope_id: Annotated[
        int | None,
        Query(
            description=(
                "Narrow by the scoped object's primary key "
                "(cell_id, subcategory_id, etc.).  Only meaningful when "
                "``scope_type`` is also given."
            )
        ),
    ] = None,
    audience: Annotated[
        str | None,
        Query(
            description=(
                "Explicit audience narrowing filter: ``all | analyst | business | external``. "
                "When omitted the server applies the role-based visibility rules "
                "automatically.  Supplying a value that is outside the viewer's visible "
                "set returns an empty result (not an error)."
            )
        ),
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=200, description="Page size (max 200).")
    ] = 50,
    offset: Annotated[
        int, Query(ge=0, description="Zero-based row offset.")
    ] = 0,
) -> CommentaryList:
    """Paginated commentary list, automatically filtered by audience visibility.

    The internal ``_engagement_settings`` rows written by the settings router
    are always excluded — they are accessible only through ``GET /settings/*``.

    Rows are returned newest-first (``created_at DESC``, then
    ``commentary_id DESC`` for a stable sort within the same second).

    **Audience filtering** is applied in SQL (not in Python) so pagination
    counts are accurate.  The viewer's role determines which ``audience`` tag
    values are allowed; an optional ``audience`` query parameter further
    narrows the result to a single tag within the viewer's permitted set.
    """
    # Compute the set of audience tags the viewer is permitted to read.
    allowed_tags = visible_audience_tags(user.role)

    # Optional explicit audience narrowing — still bounded by the viewer's role.
    if audience is not None:
        _validate_audience(audience)
        effective_tags = allowed_tags & {audience}
    else:
        effective_tags = allowed_tags

    # Validate optional scope_type before building the query.
    if scope_type is not None:
        _validate_scope_type(scope_type)

    # Base filter: exclude the internal settings sentinel and apply audience gate.
    base_filters = [
        Commentary.scope_type != _SETTINGS_SCOPE,
        Commentary.audience.in_(list(effective_tags)),
    ]
    if scope_type is not None:
        base_filters.append(Commentary.scope_type == scope_type)
    if scope_id is not None:
        base_filters.append(Commentary.scope_id == scope_id)

    # COUNT — separate lightweight query for accurate pagination total.
    count_stmt = (
        select(func.count(Commentary.commentary_id))
        .where(*base_filters)
    )
    total: int = db.execute(count_stmt).scalar_one()

    # DATA — ordered, paginated.
    data_stmt = (
        select(Commentary)
        .where(*base_filters)
        .order_by(
            Commentary.created_at.desc(),
            Commentary.commentary_id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(data_stmt).scalars().all()

    logger.debug(
        "list_commentary user=%s role=%s scope_type=%s scope_id=%s "
        "effective_tags=%s total=%d returned=%d",
        user.id,
        user.role,
        scope_type,
        scope_id,
        sorted(effective_tags),
        total,
        len(rows),
    )

    return CommentaryList(
        items=[CommentaryOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# POST /api/commentary — create
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CommentaryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a commentary row",
    dependencies=[Depends(require_role("owner", "admin", "analyst"))],
)
def create_commentary(
    body: CommentaryCreate,
    db: DbSession,
    user: CurrentUserDep,
) -> CommentaryOut:
    """Create a new commentary row for the given scope.

    **Audience rules on creation:**

    * ``all``      — any writer role (analyst/admin/owner).
    * ``analyst``  — any writer role; visible only to analysts and above.
    * ``business`` — any writer role; visible to business audience + analysts.
    * ``external`` — **owner/admin only**.  External-tagged rows may be
      shared with partners outside the engagement, so they require elevated
      approval.

    The ``author`` field is set automatically from the authenticated user's
    ``id``; the ``created_at`` timestamp is server-side UTC.

    Requires ``analyst``, ``admin``, or ``owner`` role.
    """
    _validate_scope_type(body.scope_type)
    _validate_audience(body.audience)
    _check_external_permission(body.audience, user)

    row = Commentary(
        scope_type=body.scope_type,
        scope_id=body.scope_id,
        body_markdown=body.body_markdown,
        audience=body.audience,
        author=user.id,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(row)
    db.flush()    # materialise PK before refresh
    db.refresh(row)

    logger.info(
        "commentary %d created by user=%s scope_type=%s scope_id=%s audience=%s",
        row.commentary_id,
        user.id,
        row.scope_type,
        row.scope_id,
        row.audience,
    )
    return CommentaryOut.model_validate(row)


# ---------------------------------------------------------------------------
# GET /api/commentary/{commentary_id} — single row
# ---------------------------------------------------------------------------


@router.get(
    "/{commentary_id}",
    response_model=CommentaryOut,
    summary="Get a single commentary row",
)
def get_commentary(
    commentary_id: int,
    db: DbSession,
    user: CurrentUserDep,
) -> CommentaryOut:
    """Return a single commentary row if it is visible to the current user.

    Returns HTTP 404 for rows that are outside the viewer's audience visibility
    **and** for genuinely missing rows — the same response in both cases to
    avoid disclosing the existence of restricted content.

    The internal ``_engagement_settings`` sentinel rows are never surfaced here.
    """
    row = _require_commentary(commentary_id, db)

    # Block the internal settings namespace.
    if row.scope_type == _SETTINGS_SCOPE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Commentary {commentary_id} not found.",
        )

    # Audience gate: treat invisible rows identically to missing rows.
    if not is_commentary_visible(row.audience, user.role):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Commentary {commentary_id} not found.",
        )

    return CommentaryOut.model_validate(row)


# ---------------------------------------------------------------------------
# PUT /api/commentary/{commentary_id} — update
# ---------------------------------------------------------------------------


@router.put(
    "/{commentary_id}",
    response_model=CommentaryOut,
    summary="Update a commentary row (body_markdown and/or audience)",
    dependencies=[Depends(require_role("owner", "admin", "analyst"))],
)
def update_commentary(
    commentary_id: int,
    body: CommentaryUpdate,
    db: DbSession,
    user: CurrentUserDep,
) -> CommentaryOut:
    """Update ``body_markdown`` and/or ``audience`` on an existing commentary row.

    Only the two writable fields are accepted.  ``scope_type``, ``scope_id``,
    ``author``, and ``created_at`` are immutable after creation — re-POST to
    create a new revision if the scope must change.

    At least one of ``body_markdown`` or ``audience`` must be supplied.

    **Audience escalation**: only ``owner``/``admin`` may set ``audience`` to
    ``external``.

    Returns 404 for rows outside the viewer's audience visibility (same
    response as missing rows — no information disclosure).

    Requires ``analyst``, ``admin``, or ``owner`` role.
    """
    if body.body_markdown is None and body.audience is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "At least one of 'body_markdown' or 'audience' must be provided."
            ),
        )

    row = _require_commentary(commentary_id, db)

    # Block the internal settings namespace.
    if row.scope_type == _SETTINGS_SCOPE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Commentary {commentary_id} not found.",
        )

    # Audience gate: treat invisible rows as not found.
    if not is_commentary_visible(row.audience, user.role):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Commentary {commentary_id} not found.",
        )

    # Apply audience change first (validation before any mutation).
    if body.audience is not None:
        _validate_audience(body.audience)
        _check_external_permission(body.audience, user)
        row.audience = body.audience

    if body.body_markdown is not None:
        row.body_markdown = body.body_markdown

    db.flush()
    db.refresh(row)

    logger.info(
        "commentary %d updated by user=%s (audience=%s body_changed=%s)",
        commentary_id,
        user.id,
        row.audience,
        body.body_markdown is not None,
    )
    return CommentaryOut.model_validate(row)


# ---------------------------------------------------------------------------
# DELETE /api/commentary/{commentary_id} — hard delete
# ---------------------------------------------------------------------------


@router.delete(
    "/{commentary_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Hard-delete a commentary row (admin / owner only)",
)
def delete_commentary(
    commentary_id: int,
    db: DbSession,
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> None:
    """Hard-delete a commentary row.

    Unlike assumptions (which are versioned via ``superseded_by`` and never
    overwritten), commentary rows may be deleted when they are no longer
    needed.  Deletion is permanent.

    Returns 404 for genuinely missing rows **and** for the internal
    ``_engagement_settings`` sentinel (which is managed by the settings router
    and must not be deleted through this endpoint).

    Requires ``owner`` or ``admin`` role.
    """
    row = _require_commentary(commentary_id, db)

    # Protect the internal settings namespace from deletion via the CRUD API.
    if row.scope_type == _SETTINGS_SCOPE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Commentary {commentary_id} not found.",
        )

    db.delete(row)
    db.flush()

    logger.info(
        "commentary %d hard-deleted by user=%s (was scope_type=%s audience=%s)",
        commentary_id,
        user.id,
        row.scope_type,
        row.audience,
    )
    # 204 No Content — FastAPI handles the empty response body.
