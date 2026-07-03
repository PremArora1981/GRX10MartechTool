"""Engagements router — the multi-engagement control surface.

Routes (auto-registered by main.register_routers):
  GET  /engagements                 list active (and archived) engagements
  GET  /engagements/current         the engagement resolved for this request
  POST /engagements                 create a new engagement from a confirmed brief plan
  POST /engagements/{id}/activate   set the engagement cookie (switcher)
  POST /engagements/{id}/archive    soft-archive (blocked for the protected demo)
  POST /engagements/{id}/populate   launch the web-search auto-seed for planned cells

Creation materializes taxonomy + geographies + per-engagement sources + a capped
grid of ``planned`` cells (see services.engagement_materialize). The web-search
seed that puts LOW-confidence numbers on those cells is a separate, cost-gated
step (services.seed_job) so the create call stays fast.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from backend.app.deps import CurrentUserDep, DbSession, EngagementDep, DEFAULT_ENGAGEMENT_ID
from backend.app.schemas import EngagementCreate, EngagementOut
from backend.app.services import engagement_materialize, seed_job

logger = logging.getLogger("grx10.routers.engagements")

router = APIRouter(prefix="/engagements", tags=["engagements"])

_COOKIE = "engagement_id"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


class CreateResult(BaseModel):
    engagement_id: str
    name: str
    families: int
    subcategories: int
    geographies: int
    sources: int
    planned_cells: int
    capped: bool
    web_search_enabled: bool


class PopulateResult(BaseModel):
    engagement_id: str
    launched: bool
    mode: str
    planned_cells: int
    detail: str


def _engagement_out(row) -> EngagementOut:
    return EngagementOut(
        engagement_id=row.engagement_id,
        name=row.name,
        is_demo=row.is_demo,
        status=row.status,
        active_profile=row.active_profile,
        web_search_enabled=row.web_search_enabled,
        brief_text=row.brief_text,
        created_at=row.created_at,
    )


@router.get("", response_model=list[EngagementOut], summary="List engagements")
def list_engagements(
    db: DbSession,
    _user: CurrentUserDep,
    include_archived: bool = False,
) -> list[EngagementOut]:
    where = "" if include_archived else "WHERE status = 'active'"
    rows = db.execute(
        text(
            "SELECT engagement_id, name, is_demo, status, active_profile, "
            "       web_search_enabled, brief_text, created_at "
            f"FROM engagements {where} "
            "ORDER BY is_demo DESC, created_at"
        )
    ).all()
    return [_engagement_out(r) for r in rows]


@router.get("/current", response_model=EngagementOut, summary="The active engagement")
def current_engagement(
    db: DbSession,
    engagement_id: EngagementDep,
    _user: CurrentUserDep,
) -> EngagementOut:
    row = db.execute(
        text(
            "SELECT engagement_id, name, is_demo, status, active_profile, "
            "       web_search_enabled, brief_text, created_at "
            "FROM engagements WHERE engagement_id = :e"
        ),
        {"e": engagement_id},
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Engagement not found.")
    return _engagement_out(row)


@router.post("", response_model=CreateResult, status_code=status.HTTP_201_CREATED,
             summary="Create an engagement from a confirmed brief plan")
def create_engagement(
    body: EngagementCreate,
    db: DbSession,
    _user: CurrentUserDep,
    response: Response,
) -> CreateResult:
    """Materialize a new engagement and switch to it (sets the engagement cookie).

    The heavy web-search seeding is NOT done here — the client shows the cost
    banner and calls POST /engagements/{id}/populate on consent.
    """
    if not body.name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Engagement name is required.")
    try:
        summary = engagement_materialize.materialize_engagement(
            db,
            name=body.name.strip(),
            brief_text=body.brief_text,
            geographies=body.geographies,
            year_from=body.year_from,
            year_to=body.year_to,
            plan=body.plan,
            families=body.families,
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("engagement materialize failed: %s", exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create the engagement. Check server logs.",
        ) from exc

    # Switch the browser to the new engagement.
    response.set_cookie(_COOKIE, summary["engagement_id"], max_age=_COOKIE_MAX_AGE,
                        samesite="lax", path="/")
    return CreateResult(**summary)


@router.post("/{engagement_id}/activate", response_model=EngagementOut,
             summary="Switch the active engagement (sets the cookie)")
def activate_engagement(
    engagement_id: str,
    db: DbSession,
    _user: CurrentUserDep,
    response: Response,
) -> EngagementOut:
    row = db.execute(
        text(
            "SELECT engagement_id, name, is_demo, status, active_profile, "
            "       web_search_enabled, brief_text, created_at "
            "FROM engagements WHERE engagement_id = :e AND status = 'active'"
        ),
        {"e": engagement_id},
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Engagement not found or archived.")
    response.set_cookie(_COOKIE, engagement_id, max_age=_COOKIE_MAX_AGE,
                        samesite="lax", path="/")
    return _engagement_out(row)


@router.post("/{engagement_id}/archive", response_model=EngagementOut,
             summary="Soft-archive an engagement (demo is protected)")
def archive_engagement(
    engagement_id: str,
    db: DbSession,
    _user: CurrentUserDep,
    response: Response,
) -> EngagementOut:
    row = db.execute(
        text("SELECT is_demo, status FROM engagements WHERE engagement_id = :e"),
        {"e": engagement_id},
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Engagement not found.")
    if row.is_demo:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="The demo engagement is protected and cannot be archived.")
    db.execute(
        text("UPDATE engagements SET status = 'archived' WHERE engagement_id = :e"),
        {"e": engagement_id},
    )
    db.commit()
    # If the archived engagement was active in this browser, fall back to default.
    response.set_cookie(_COOKIE, DEFAULT_ENGAGEMENT_ID, max_age=_COOKIE_MAX_AGE,
                        samesite="lax", path="/")
    updated = db.execute(
        text(
            "SELECT engagement_id, name, is_demo, status, active_profile, "
            "       web_search_enabled, brief_text, created_at "
            "FROM engagements WHERE engagement_id = :e"
        ),
        {"e": engagement_id},
    ).first()
    return _engagement_out(updated)


@router.post("/{engagement_id}/populate", response_model=PopulateResult,
             summary="Launch the web-search auto-seed for this engagement's planned cells")
def populate_engagement(
    engagement_id: str,
    db: DbSession,
    _user: CurrentUserDep,
    background: BackgroundTasks,
) -> PopulateResult:
    """Kick off web-search sizing of the engagement's ``planned`` cells (cost-gated
    by the client). Delegates to services.seed_job, which runs a Render one-off
    Job in the cloud or an in-process background task locally."""
    exists = db.execute(
        text("SELECT 1 FROM engagements WHERE engagement_id = :e AND status = 'active'"),
        {"e": engagement_id},
    ).first()
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Engagement not found or archived.")
    n_planned = db.execute(
        text("SELECT COUNT(*) FROM cells WHERE engagement_id = :e AND status = 'planned'"),
        {"e": engagement_id},
    ).scalar_one()
    result = seed_job.launch_seed(engagement_id, background=background)
    return PopulateResult(
        engagement_id=engagement_id,
        launched=result["launched"],
        mode=result["mode"],
        planned_cells=int(n_planned),
        detail=result["detail"],
    )
