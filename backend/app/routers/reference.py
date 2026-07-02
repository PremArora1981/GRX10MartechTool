"""Reference / taxonomy lookup API — ``/reference/*``.

Read-only reference data used to populate scope pickers and lookups across the
UI (most notably the Assumptions Ledger's geography / subcategory / company /
method selectors). Everything here is public reference metadata — no secrets —
so the endpoints are ungated and cached ``no-store`` by the frontend.

Routes (auto-registered by ``main.py`` router discovery):
    GET /reference/geographies    — country × segment rows
    GET /reference/subcategories  — taxonomy subcategories (active head rows)
    GET /reference/companies      — seeded + discovered players
    GET /reference/methods        — method registry (tiers, source class, caps)

Field shapes mirror the frontend ``Geography``/``TaxonomySubcategory``/
``Company``/``MethodRegistryEntry`` types in ``frontend/lib/types.ts`` exactly.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from sqlalchemy import text

from backend.app.deps import DbSession

logger = logging.getLogger("grx10.routers.reference")

router = APIRouter(prefix="/reference", tags=["reference"])


def _rows(session: DbSession, sql: str) -> list[dict[str, Any]]:
    return [dict(r) for r in session.execute(text(sql)).mappings().all()]


@router.get("/geographies", response_model=None, summary="Geographies (country × segment)")
def list_geographies(session: DbSession) -> list[dict[str, Any]]:
    """All geography rows, ordered by country then segment."""
    return _rows(
        session,
        """
        SELECT geography_id, country, segment
        FROM geographies
        ORDER BY country, segment
        """,
    )


@router.get("/subcategories", response_model=None, summary="Taxonomy subcategories")
def list_subcategories(session: DbSession) -> list[dict[str, Any]]:
    """Taxonomy subcategories.

    ``hs_codes`` / ``regulatory_codes`` are text arrays (coerced to ``[]`` when
    NULL so the frontend can iterate safely). Ordered by family then name.
    """
    rows = _rows(
        session,
        """
        SELECT subcategory_id, family_id, name,
               COALESCE(hs_codes, '{}') AS hs_codes,
               COALESCE(regulatory_codes, '{}') AS regulatory_codes,
               version, superseded_by
        FROM taxonomy_subcategories
        ORDER BY family_id, name
        """,
    )
    for r in rows:
        r["hs_codes"] = list(r.get("hs_codes") or [])
        r["regulatory_codes"] = list(r.get("regulatory_codes") or [])
    return rows


@router.get("/companies", response_model=None, summary="Companies (seeded + discovered players)")
def list_companies(session: DbSession) -> list[dict[str, Any]]:
    """All companies, ordered by name."""
    return _rows(
        session,
        """
        SELECT company_id, name, company_type, country_hq, seeded_role, discovered
        FROM companies
        ORDER BY name
        """,
    )


@router.get("/methods", response_model=None, summary="Method registry")
def list_methods(session: DbSession) -> list[dict[str, Any]]:
    """Method registry entries (estimation methods with tier + source class)."""
    rows = _rows(
        session,
        """
        SELECT method_code, description, tier, source_class, is_primary_source,
               confidence_cap,
               COALESCE(required_raw_tables, '{}') AS required_raw_tables
        FROM method_registry
        ORDER BY tier, method_code
        """,
    )
    for r in rows:
        r["required_raw_tables"] = list(r.get("required_raw_tables") or [])
    return rows
