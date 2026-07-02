"""Sources registry API — /sources/*.

Two endpoints surface the registered source registry with per-source
evidence rationale and method-feed maps for the Sources View (W2):

GET /sources
    Every row in the sources table with:
    - source_id, publisher, class (A/B/C), access_method, enabled,
      last_probe_status, last_probe_detail, raw_table, notes
    - ``why`` — rationale: uses notes when present, otherwise a
      class-based default (A = HIGH evidence; B = MEDIUM; C = scaling).
    - ``used_for`` — list of method_codes whose required_raw_tables
      contain this source's raw_table (join method_registry).

GET /sources/recommended?family=<name>
    Returns a method_code → [source_id, ...] map showing which enabled
    sources feed each method.  The optional ``family`` query-param is
    accepted and stored for future use but does not currently filter
    because the sources-to-method mapping is table-based (raw_table),
    not family-scoped.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text

from backend.app.deps import DbSession, CurrentUserDep

router = APIRouter(prefix="/sources", tags=["sources"])

# ---------------------------------------------------------------------------
# Class → default "why it matters" description
# ---------------------------------------------------------------------------

_CLASS_WHY: dict[str, str] = {
    "A": "Primary structured evidence — qualifies HIGH confidence",
    "B": "Industry/procedural cross-check — qualifies MEDIUM confidence",
    "C": "Triangulation support — gap-fill/scaling only",
}


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class SourceDetailOut(BaseModel):
    """One source row enriched with rationale and method usage."""

    source_id: str
    publisher: str
    source_class: str | None = None     # A | B | C
    access_method: str | None = None    # api | scrape | web_search | manual_upload
    raw_table: str | None = None
    enabled: bool | None = None
    last_probe_status: str | None = None
    last_probe_detail: str | None = None
    notes: str | None = None
    why: str                            # plain-English rationale line
    used_for: list[str]                 # method_codes whose raw_tables include this source


class RecommendedSourcesOut(BaseModel):
    """Map of method_code → enabled source_ids that feed it."""

    method_map: dict[str, list[str]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_raw_table_to_methods(methods_rows: list[Any]) -> dict[str, list[str]]:
    """Build raw_table → [method_code] index from method_registry rows."""
    index: dict[str, list[str]] = {}
    for row in methods_rows:
        tables: list[str] = row.required_raw_tables or []
        for tbl in tables:
            if tbl:
                index.setdefault(tbl, []).append(row.method_code)
    return index


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/recommended",
    response_model=RecommendedSourcesOut,
    summary="Recommended sources per method — method_code → [source_ids]",
)
def get_recommended_sources(
    db: DbSession,
    _user: CurrentUserDep,
    family: str | None = Query(
        default=None,
        description="Optional taxonomy family name (reserved for future scoping).",
    ),
) -> RecommendedSourcesOut:
    """Return a map of method_code to the enabled source_ids that feed it.

    The mapping is derived from ``method_registry.required_raw_tables``
    joined with ``sources.raw_table``.  Only enabled sources are included.
    The ``family`` parameter is accepted for forward-compatibility but does
    not yet filter the result (family→source mapping is not stored in the DB).
    """
    methods_rows = db.execute(
        text("""
            SELECT method_code, required_raw_tables
            FROM method_registry
            WHERE required_raw_tables IS NOT NULL
              AND array_length(required_raw_tables, 1) > 0
            ORDER BY method_code
        """)
    ).fetchall()

    sources_rows = db.execute(
        text("""
            SELECT source_id, raw_table
            FROM sources
            WHERE raw_table IS NOT NULL
              AND enabled IS NOT FALSE
        """)
    ).fetchall()

    # Build raw_table → [source_id] index (enabled sources only).
    raw_table_to_sources: dict[str, list[str]] = {}
    for row in sources_rows:
        raw_table_to_sources.setdefault(row.raw_table, []).append(row.source_id)

    method_map: dict[str, list[str]] = {}
    for mrow in methods_rows:
        tables: list[str] = mrow.required_raw_tables or []
        seen: set[str] = set()
        source_ids: list[str] = []
        for tbl in tables:
            for sid in raw_table_to_sources.get(tbl, []):
                if sid not in seen:
                    seen.add(sid)
                    source_ids.append(sid)
        method_map[mrow.method_code] = source_ids

    return RecommendedSourcesOut(method_map=method_map)


@router.get(
    "",
    response_model=list[SourceDetailOut],
    summary="Source registry — every source with class, health, rationale, and method usage",
)
def list_sources(
    db: DbSession,
    _user: CurrentUserDep,
) -> list[SourceDetailOut]:
    """Return every row of the sources table enriched with:

    * **why** — a plain-English rationale.  Uses the source's ``notes``
      when present; falls back to a class-level description (A/B/C).
    * **used_for** — the method_codes whose ``required_raw_tables``
      contain this source's ``raw_table`` (derived via method_registry).

    Results are ordered by class (A first) then publisher alphabetically.
    """
    sources_rows = db.execute(
        text("""
            SELECT source_id, publisher, class AS source_class, access_method,
                   raw_table, enabled, last_probe_status, last_probe_detail, notes
            FROM sources
            ORDER BY
                CASE class WHEN 'A' THEN 1 WHEN 'B' THEN 2 WHEN 'C' THEN 3 ELSE 4 END,
                publisher
        """)
    ).fetchall()

    methods_rows = db.execute(
        text("""
            SELECT method_code, required_raw_tables
            FROM method_registry
            WHERE required_raw_tables IS NOT NULL
        """)
    ).fetchall()

    raw_table_to_methods = _build_raw_table_to_methods(methods_rows)

    result: list[SourceDetailOut] = []
    for s in sources_rows:
        s_class: str | None = s.source_class
        notes: str | None = s.notes

        # Rationale: notes first, then class default.
        if notes and notes.strip():
            why = notes.strip()
        else:
            why = _CLASS_WHY.get(s_class or "", "Data source — consult connector documentation")

        used_for = raw_table_to_methods.get(s.raw_table or "", [])

        result.append(
            SourceDetailOut(
                source_id=s.source_id,
                publisher=s.publisher,
                source_class=s_class,
                access_method=s.access_method,
                raw_table=s.raw_table,
                enabled=s.enabled,
                last_probe_status=s.last_probe_status,
                last_probe_detail=s.last_probe_detail,
                notes=notes,
                why=why,
                used_for=used_for,
            )
        )

    return result
