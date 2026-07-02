"""Estimation-method framework — the contract every sizing method implements.

A *method* turns raw, normalised source data into one or more independent TAM
estimates for a single cell (subcategory x geography x year). The pipeline's
``size_cells`` stage runs every registered method against every active cell and
upserts the results into ``cell_triangulation``; the confidence engine
(``cell_triangulation_summary``) then decides HIGH/MEDIUM/LOW from how many
*distinct methods x source-classes* agree, and how tightly.

Contract (matches ``pipeline/run.py`` exactly)::

    class Method(ABC):
        method_code: str
        required_raw_tables: list[str]
        def estimate(self, cell: dict, session) -> list[dict]

Each returned dict becomes one ``cell_triangulation`` row and MUST carry:

    {"method_code": str,            # defaults to self.method_code if omitted
     "estimate_usd_m": Decimal/num, # the TAM estimate in USD millions
     "source_id": str,              # REQUIRED — no fact row without a source
     "notes": str | None}

Invariants enforced/encouraged here:

* **No fact row without a non-null ``source_id``.** :meth:`row` refuses to build
  a result without one, so a method physically cannot emit a sourceless estimate.
* **Never fabricate.** When the required raw tables are empty for a cell, a
  method must return ``[]`` — not a guessed number.
* Confidence is **never** set by a method (it is computed only by the summary
  view); methods only contribute estimates + their source.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger("grx10.methods")


class Method(ABC):
    """Abstract base class for estimation methods.

    Subclasses set :attr:`method_code` (matching ``method_registry.method_code``)
    and :attr:`required_raw_tables`, then implement :meth:`estimate`.
    """

    #: Stable code matching a row in ``method_registry``.
    method_code: str = ""
    #: Raw tables this method reads (used to skip it when its inputs are empty).
    required_raw_tables: list[str] = []

    # ------------------------------------------------------------------ #
    # Contract
    # ------------------------------------------------------------------ #
    @abstractmethod
    def estimate(self, cell: dict[str, Any], session: Connection) -> list[dict[str, Any]]:
        """Return zero or more triangulation rows for ``cell``.

        ``cell`` carries the cell + its spine context, as assembled by the
        pipeline: ``cell_id``, ``subcategory_id``, ``geography_id``, ``year``,
        ``subcategory_name``, ``hs_codes``, ``regulatory_codes``, ``country``,
        ``segment``. ``session`` is an open SQLAlchemy :class:`Connection` for
        reading the ``raw_*`` and spine tables.

        Returns an empty list when there is no defensible estimate (no raw data
        for the cell) — methods must never fabricate a number.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Helpers for subclasses
    # ------------------------------------------------------------------ #
    def row(
        self,
        *,
        estimate_usd_m: Any,
        source_id: str,
        notes: str | None = None,
        method_code: str | None = None,
    ) -> dict[str, Any]:
        """Build one validated triangulation result dict.

        Raises ``ValueError`` if ``source_id`` is missing (enforcing the "no fact
        row without a source" invariant) or the estimate is not numeric.
        """
        if not source_id:
            raise ValueError(
                f"{self.method_code}: refusing to emit an estimate without a source_id"
            )
        amount = self._to_decimal(estimate_usd_m)
        if amount is None:
            raise ValueError(
                f"{self.method_code}: estimate_usd_m {estimate_usd_m!r} is not numeric"
            )
        return {
            "method_code": method_code or self.method_code,
            "estimate_usd_m": amount,
            "source_id": source_id,
            "notes": notes,
        }

    def has_required_data(self, session: Connection, cell: dict[str, Any]) -> bool:
        """True when at least one required raw table holds any row.

        A cheap pre-check so :meth:`estimate` can early-return ``[]`` instead of
        fabricating when its inputs are empty. (Coarse by design — per-cell
        filtering is the method's own job.)
        """
        for table in self.required_raw_tables:
            try:
                exists = session.execute(
                    text(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608 - table from trusted registry
                ).first()
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s: required-data check on %s failed: %s",
                             self.method_code, table, exc)
                continue
            if exists:
                return True
        return False

    @staticmethod
    def _to_decimal(value: Any) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} method_code={self.method_code!r}>"
