"""Audience-visibility helper (Q10 — audience switcher).

The ``commentary`` table's ``audience`` column tags each row as visible to one
of four tiers: ``all | analyst | business | external``.  The user's role (from
WorkOS, mapped in ``deps.CurrentUser.role``) determines which tags they can see.

Visibility matrix
-----------------
+-----------------+-----+----------+----------+----------+
| viewer role     | all | analyst  | business | external |
+=================+=====+==========+==========+==========+
| owner / admin   |  v  |    v     |    v     |    v     |
| analyst         |  v  |    v     |          |          |
| business        |  v  |          |    v     |          |
| external        |  v  |          |          |    v     |
+-----------------+-----+----------+----------+----------+

The same logic gates assumption detail and triangulation detail:

* **Assumption detail**: ``analyst``/``owner``/``admin`` see all fields including
  ``derivation_method``, ``source_id``, and numeric breakdown.  ``business`` and
  ``external`` see only ``assumption_text`` + effective year range — no internal
  derivation scaffolding that would expose the analytical methodology.

* **Triangulation detail**: ``analyst``/``owner``/``admin`` see individual
  per-method estimates (the Cell Detail drill chain from cell -> estimate ->
  source -> raw payload).  ``business`` and ``external`` see only the summarised
  TAM band (``tam_low / tam_revenue / tam_high``) and confidence chip — not the
  raw per-method breakdown nor source attribution.

These functions are **pure** (no DB I/O) so they can be called anywhere:
the reports module, commentary router, assumptions router, and frontend API
adapters.  Import is always safe regardless of request context.

Usage::

    from backend.app.services.audience import (
        visible_audience_tags,
        is_commentary_visible,
        filter_commentary_rows,
        can_see_assumption_detail,
        redact_assumption,
        can_see_triangulation_detail,
    )
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("grx10.services.audience")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: All valid audience tag values (mirrors the ``commentary.audience`` domain).
VALID_AUDIENCE_TAGS: frozenset[str] = frozenset({"all", "analyst", "business", "external"})

#: Roles that have full analytical access (equivalent visibility for content).
_ANALYST_OR_ABOVE: frozenset[str] = frozenset({"owner", "admin", "analyst"})

#: Mapping of user role -> set of commentary audience tags they may read.
#: Unknown roles fall through to the ``external`` tier (most restrictive).
_ROLE_VISIBLE_TAGS: dict[str, frozenset[str]] = {
    "owner":    frozenset({"all", "analyst", "business", "external"}),
    "admin":    frozenset({"all", "analyst", "business", "external"}),
    "analyst":  frozenset({"all", "analyst"}),
    "business": frozenset({"all", "business"}),
    "external": frozenset({"all", "external"}),
}

#: Technical assumption fields that are hidden from business/external audiences.
#: These carry internal analytical scaffolding not suitable for external disclosure.
_REDACTED_ASSUMPTION_FIELDS: tuple[str, ...] = (
    "derivation_method",
    "source_id",
    "numeric_value",
    "unit",
    "confidence",
)


# ---------------------------------------------------------------------------
# Commentary visibility
# ---------------------------------------------------------------------------

def visible_audience_tags(role: str) -> frozenset[str]:
    """Return the set of commentary ``audience`` tag values the given role may see.

    Unknown roles are treated as ``external`` (most restrictive fallback).

    Args:
        role: The ``CurrentUser.role`` string
            (one of owner/admin/analyst/business/external).

    Returns:
        A frozenset of audience tag values the role is allowed to read.

    Examples::

        >>> visible_audience_tags("analyst")
        frozenset({'all', 'analyst'})
        >>> visible_audience_tags("business")
        frozenset({'all', 'business'})
        >>> visible_audience_tags("unknown")   # defaults to external
        frozenset({'all', 'external'})
    """
    return _ROLE_VISIBLE_TAGS.get(role, _ROLE_VISIBLE_TAGS["external"])


def is_commentary_visible(commentary_audience: str | None, viewer_role: str) -> bool:
    """Return True if a commentary row is visible to the given viewer role.

    ``None`` or empty ``audience`` is treated as ``"all"`` (maximally visible
    — a row with no audience restriction is always surfaced).

    Args:
        commentary_audience: The ``commentary.audience`` column value.
        viewer_role: The ``CurrentUser.role`` string.

    Returns:
        True when the row should be surfaced to this viewer.

    Examples::

        >>> is_commentary_visible("analyst", "business")
        False
        >>> is_commentary_visible("all", "external")
        True
        >>> is_commentary_visible(None, "analyst")
        True
    """
    tag = commentary_audience or "all"
    return tag in visible_audience_tags(viewer_role)


def filter_commentary_rows(rows: list[Any], viewer_role: str) -> list[Any]:
    """Filter a list of Commentary ORM objects (or dicts) by audience visibility.

    Accepts either ORM ``Commentary`` instances (with an ``audience`` attribute)
    or plain dicts (with an ``"audience"`` key), making the function usable
    inside SQLAlchemy sessions and on pre-serialised payloads alike.

    Rows with no audience value are treated as ``"all"`` and are always included.

    Args:
        rows: List of Commentary ORM instances or serialised dicts.
        viewer_role: The ``CurrentUser.role`` string.

    Returns:
        A new list containing only the rows visible to the given role.  The
        original list and its elements are not mutated.
    """
    allowed = visible_audience_tags(viewer_role)
    result: list[Any] = []
    for row in rows:
        if isinstance(row, dict):
            tag: str = row.get("audience") or "all"
        else:
            tag = getattr(row, "audience", None) or "all"
        if tag in allowed:
            result.append(row)

    logger.debug(
        "filter_commentary_rows: role=%s in=%d out=%d",
        viewer_role,
        len(rows),
        len(result),
    )
    return result


# ---------------------------------------------------------------------------
# Assumption detail visibility
# ---------------------------------------------------------------------------

def can_see_assumption_detail(role: str) -> bool:
    """Return True if the given role may see technical assumption detail fields.

    Technical fields (``derivation_method``, ``source_id``, ``numeric_value``,
    ``unit``, ``confidence``) carry internal analytical scaffolding and are not
    appropriate for business or external audiences.

    ``business`` and ``external`` users see only:

    * ``assumption_text`` — the human-readable statement
    * ``effective_from_year`` / ``effective_to_year`` — validity window
    * ``assumption_id``, ``scope_*`` IDs — for navigation and linking
    * ``superseded_by`` / ``created_at`` — version chain metadata

    Args:
        role: The ``CurrentUser.role`` string.

    Returns:
        True for owner/admin/analyst; False for business/external.
    """
    return role in _ANALYST_OR_ABOVE


def redact_assumption(assumption: dict[str, Any], viewer_role: str) -> dict[str, Any]:
    """Strip internal technical fields from a serialised assumption dict.

    When the viewer ``can_see_assumption_detail``, the assumption dict is
    returned unchanged (the same object reference — no copy).  Otherwise the
    internal analytical fields listed in ``_REDACTED_ASSUMPTION_FIELDS`` are
    replaced with ``None`` in a shallow copy so the structure is preserved but
    the values are withheld.

    This function is designed to operate on the output of
    ``AssumptionOut.model_dump()`` so the reports module and assumption list
    endpoints can apply consistent redaction before serialising to JSON.

    Args:
        assumption: A dict representation of an assumption row.
        viewer_role: The ``CurrentUser.role`` string.

    Returns:
        The original dict (unchanged) if the viewer has detail access, otherwise
        a new shallow-copy dict with technical fields set to ``None``.

    Examples::

        >>> row = {"assumption_id": 1, "assumption_text": "...", "source_id": "edinet", ...}
        >>> redact_assumption(row, "business")
        {"assumption_id": 1, "assumption_text": "...", "source_id": None, ...}
        >>> redact_assumption(row, "analyst") is row   # same object, no copy
        True
    """
    if can_see_assumption_detail(viewer_role):
        return assumption
    redacted = dict(assumption)
    for field in _REDACTED_ASSUMPTION_FIELDS:
        redacted[field] = None
    return redacted


# ---------------------------------------------------------------------------
# Triangulation / method-estimate visibility
# ---------------------------------------------------------------------------

def can_see_triangulation_detail(role: str) -> bool:
    """Return True if the given role may see individual per-method estimates.

    Analysts (and above) can drill all the way from TAM to individual method
    estimates, source URLs, and raw payload references — the full two-click
    audit chain mandated by the spec.

    ``business`` and ``external`` viewers see only the cell's summarised TAM
    band (``tam_low_usd_m`` / ``tam_revenue_usd_m`` / ``tam_high_usd_m``) and
    confidence chip.  The ``estimates`` list in ``CellDetail`` and the
    ``/triangulation-summary`` endpoint should be omitted or replaced with a
    stub for these roles.

    Args:
        role: The ``CurrentUser.role`` string.

    Returns:
        True for owner/admin/analyst; False for business/external.
    """
    return role in _ANALYST_OR_ABOVE


__all__ = [
    "VALID_AUDIENCE_TAGS",
    # commentary visibility
    "visible_audience_tags",
    "is_commentary_visible",
    "filter_commentary_rows",
    # assumption visibility
    "can_see_assumption_detail",
    "redact_assumption",
    # triangulation visibility
    "can_see_triangulation_detail",
]
