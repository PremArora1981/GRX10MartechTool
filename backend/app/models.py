"""SQLAlchemy 2.0 declarative models.

These map onto the **already-migrated** schema in
``db/changelog/changelog-master.sql`` (owned by Liquibase). They are a typed view
of existing tables — never a migration source. Table and column names match the
DDL exactly; where a column collides with a Python keyword (``class``) the Python
attribute is renamed and the real column name is given to ``mapped_column``.

Layers (mirroring the DDL):

* Layer 1 — Spine: taxonomy, geographies, companies, sources, method_registry,
  connector_credentials, credential_audit, validation_profiles, assumptions.
* Layer 0 — Raw: the twelve ``raw_*`` source-class tables.
* Layer 2 — Cells: cells, cell_triangulation, and the read-only
  ``cell_triangulation_summary`` materialised view.
* Layer 3 — Players: player_shares, supplier_relationships, facilities.
* Layer 4 — Decisions: catalysts, recommendations, cell_assumption_link, commentary.
"""

from __future__ import annotations

import datetime
import decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all mapped tables and views."""


# Common typing aliases for readability.
_TS = datetime.datetime
_Num = decimal.Decimal


class Engagement(Base):
    __tablename__ = "engagements"

    engagement_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    active_profile: Mapped[str] = mapped_column(Text, nullable=False, default="Standard")
    web_search_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    brief_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_TS] = mapped_column(DateTime(timezone=True), nullable=False)


# =========================================================================== #
# LAYER 1 — SPINE
# =========================================================================== #
class TaxonomyFamily(Base):
    __tablename__ = "taxonomy_families"

    family_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))

    subcategories: Mapped[list["TaxonomySubcategory"]] = relationship(
        back_populates="family", foreign_keys="TaxonomySubcategory.family_id"
    )


class TaxonomySubcategory(Base):
    __tablename__ = "taxonomy_subcategories"

    subcategory_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    family_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("taxonomy_families.family_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    hs_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    regulatory_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    superseded_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("taxonomy_subcategories.subcategory_id")
    )
    created_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))

    family: Mapped["TaxonomyFamily"] = relationship(
        back_populates="subcategories", foreign_keys=[family_id]
    )


class Geography(Base):
    __tablename__ = "geographies"
    __table_args__ = (UniqueConstraint("country", "segment"),)

    geography_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str] = mapped_column(Text, nullable=False)
    # DOMESTIC | IMPORT | EXPORT | SELF_CONSUME ...
    segment: Mapped[str] = mapped_column(Text, nullable=False)


class Company(Base):
    __tablename__ = "companies"

    company_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    company_type: Mapped[str | None] = mapped_column(Text)
    country_hq: Mapped[str | None] = mapped_column(Text)
    seeded_role: Mapped[str | None] = mapped_column(Text)
    discovered: Mapped[bool | None] = mapped_column(Boolean, default=False)
    created_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))


class Source(Base):
    __tablename__ = "sources"

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    publisher: Mapped[str] = mapped_column(Text, nullable=False)
    url_pattern: Mapped[str | None] = mapped_column(Text)
    auth: Mapped[str | None] = mapped_column(Text)             # none | api_key | oauth | login | scrape
    auth_secret_ref: Mapped[str | None] = mapped_column(Text)  # -> connector_credentials.cred_ref
    # 'class' is a Python keyword: attribute renamed, column kept as "class".
    source_class: Mapped[str | None] = mapped_column("class", String(1))
    connector: Mapped[str | None] = mapped_column(Text)
    refresh_cadence: Mapped[str | None] = mapped_column(Text)
    raw_table: Mapped[str | None] = mapped_column(Text)
    access_method: Mapped[str | None] = mapped_column(Text, default="api")  # api | scrape | web_search | manual_upload
    discovered: Mapped[bool | None] = mapped_column(Boolean, default=False)
    monthly_budget: Mapped[_Num | None] = mapped_column(Numeric(10, 2))
    quota_ceiling: Mapped[int | None] = mapped_column(Integer)
    last_probe_status: Mapped[str | None] = mapped_column(Text)
    last_probe_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))
    last_probe_detail: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool | None] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("class IN ('A','B','C')", name="sources_class_check"),
    )


class MethodRegistry(Base):
    __tablename__ = "method_registry"
    __table_args__ = (
        CheckConstraint("tier IN ('A','B','C')", name="method_registry_tier_check"),
        CheckConstraint(
            "confidence_cap IN ('high','medium','low')",
            name="method_registry_confidence_cap_check",
        ),
    )

    method_code: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[str | None] = mapped_column(String(1))
    source_class: Mapped[str | None] = mapped_column(Text)  # method x source-class independence (Q5)
    is_primary_source: Mapped[bool | None] = mapped_column(Boolean, default=False)
    confidence_cap: Mapped[str | None] = mapped_column(Text)  # e.g. web_search => low
    required_raw_tables: Mapped[list[str] | None] = mapped_column(ARRAY(Text))


class ConnectorCredential(Base):
    """Envelope-encrypted credential store (Q9) — ciphertext only, never plaintext."""

    __tablename__ = "connector_credentials"

    cred_ref: Mapped[str] = mapped_column(Text, primary_key=True)  # = sources.auth_secret_ref
    source_id: Mapped[str | None] = mapped_column(Text, ForeignKey("sources.source_id"))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)     # pgp_sym_encrypt(secret, data_key)
    enc_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)   # data key wrapped by the master key
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))


class CredentialAudit(Base):
    __tablename__ = "credential_audit"

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cred_ref: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # added | rotated | removed
    actor: Mapped[str | None] = mapped_column(Text)
    at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))


class ValidationProfile(Base):
    """Configurable confidence thresholds (Q5). Exactly one row is active."""

    __tablename__ = "validation_profiles"

    profile_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=False)
    independence_level: Mapped[str] = mapped_column(
        Text, nullable=False, default="method_x_source_class"
    )  # method | method_x_source_class
    high_min_distinct_methods: Mapped[int] = mapped_column(Integer, nullable=False)
    high_max_spread: Mapped[_Num] = mapped_column(Numeric(4, 3), nullable=False)
    high_require_tier_a: Mapped[bool] = mapped_column(Boolean, nullable=False)
    high_min_source_classes: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    medium_min_distinct_methods: Mapped[int] = mapped_column(Integer, nullable=False)
    medium_max_spread: Mapped[_Num] = mapped_column(Numeric(4, 3), nullable=False)
    medium_alt_min_methods: Mapped[int | None] = mapped_column(Integer)
    medium_alt_max_spread: Mapped[_Num | None] = mapped_column(Numeric(4, 3))


class Assumption(Base):
    __tablename__ = "assumptions"

    assumption_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope_company_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("companies.company_id"))
    scope_subcategory_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("taxonomy_subcategories.subcategory_id")
    )
    scope_geography_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("geographies.geography_id")
    )
    assumption_text: Mapped[str] = mapped_column(Text, nullable=False)
    numeric_value: Mapped[_Num | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)
    derivation_method: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(Text, ForeignKey("sources.source_id"))
    effective_from_year: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_to_year: Mapped[int | None] = mapped_column(Integer)
    superseded_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("assumptions.assumption_id")
    )
    created_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))


# =========================================================================== #
# LAYER 0 — RAW (one table per source class). They share a common shape:
# raw_id PK, source_id FK, accessed_at, raw_json (verbatim), + typed columns.
# =========================================================================== #
class _RawBase(Base):
    """Abstract mixin for the common raw-row columns (not itself a table)."""

    __abstract__ = True

    raw_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    accessed_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False)

    @property
    def source_id(self) -> str:  # pragma: no cover - overridden by concrete columns
        raise NotImplementedError


def _source_fk() -> Mapped[str]:
    return mapped_column(Text, ForeignKey("sources.source_id"), nullable=False)


class RawTradeFlow(_RawBase):
    __tablename__ = "raw_trade_flows"

    source_id: Mapped[str] = _source_fk()
    reporter: Mapped[str | None] = mapped_column(Text)
    partner: Mapped[str | None] = mapped_column(Text)
    hs_code: Mapped[str | None] = mapped_column(Text)
    hs_version: Mapped[str | None] = mapped_column(Text)
    flow: Mapped[str | None] = mapped_column(Text)
    period: Mapped[str | None] = mapped_column(Text)
    value_usd: Mapped[_Num | None] = mapped_column(Numeric)
    qty: Mapped[_Num | None] = mapped_column(Numeric)
    qty_unit: Mapped[str | None] = mapped_column(Text)


class RawRegulatory(_RawBase):
    __tablename__ = "raw_regulatory"

    source_id: Mapped[str] = _source_fk()
    registration_id: Mapped[str | None] = mapped_column(Text)
    holder: Mapped[str | None] = mapped_column(Text)
    product_code: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)


class RawFiling(_RawBase):
    __tablename__ = "raw_filings"

    source_id: Mapped[str] = _source_fk()
    filer: Mapped[str | None] = mapped_column(Text)
    ticker: Mapped[str | None] = mapped_column(Text)
    period: Mapped[str | None] = mapped_column(Text)
    segment: Mapped[str | None] = mapped_column(Text)
    geography: Mapped[str | None] = mapped_column(Text)
    revenue_usd: Mapped[_Num | None] = mapped_column(Numeric)
    doc_url: Mapped[str | None] = mapped_column(Text)


class RawTranscript(_RawBase):
    __tablename__ = "raw_transcripts"

    source_id: Mapped[str] = _source_fk()
    company: Mapped[str | None] = mapped_column(Text)
    period: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)


class RawShipment(_RawBase):
    __tablename__ = "raw_shipments"

    source_id: Mapped[str] = _source_fk()
    shipper: Mapped[str | None] = mapped_column(Text)
    consignee: Mapped[str | None] = mapped_column(Text)
    hs_code: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str | None] = mapped_column(Text)
    dest: Mapped[str | None] = mapped_column(Text)
    value_usd: Mapped[_Num | None] = mapped_column(Numeric)
    period: Mapped[str | None] = mapped_column(Text)


class RawExternalMetric(_RawBase):
    __tablename__ = "raw_external_metrics"

    source_id: Mapped[str] = _source_fk()
    indicator: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    period: Mapped[str | None] = mapped_column(Text)
    value: Mapped[_Num | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(Text)


class RawIndustryReport(_RawBase):
    __tablename__ = "raw_industry_reports"

    source_id: Mapped[str] = _source_fk()
    publisher: Mapped[str | None] = mapped_column(Text)
    market: Mapped[str | None] = mapped_column(Text)
    period: Mapped[str | None] = mapped_column(Text)
    tam_usd: Mapped[_Num | None] = mapped_column(Numeric)
    doc_url: Mapped[str | None] = mapped_column(Text)


class RawPatent(_RawBase):
    __tablename__ = "raw_patents"

    source_id: Mapped[str] = _source_fk()
    patent_id: Mapped[str | None] = mapped_column(Text)
    assignee: Mapped[str | None] = mapped_column(Text)
    cpc: Mapped[str | None] = mapped_column(Text)
    filing_date: Mapped[datetime.date | None] = mapped_column(Date)
    country: Mapped[str | None] = mapped_column(Text)


class RawProcurement(_RawBase):
    __tablename__ = "raw_procurement"

    source_id: Mapped[str] = _source_fk()
    award_id: Mapped[str | None] = mapped_column(Text)
    buyer: Mapped[str | None] = mapped_column(Text)
    supplier: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    value_usd: Mapped[_Num | None] = mapped_column(Numeric)
    period: Mapped[str | None] = mapped_column(Text)


class RawStandard(_RawBase):
    __tablename__ = "raw_standards"

    source_id: Mapped[str] = _source_fk()
    body: Mapped[str | None] = mapped_column(Text)
    member: Mapped[str | None] = mapped_column(Text)
    membership_tier: Mapped[str | None] = mapped_column(Text)


class RawNews(_RawBase):
    __tablename__ = "raw_news"

    source_id: Mapped[str] = _source_fk()
    headline: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))
    entity: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)


class RawSignal(_RawBase):
    __tablename__ = "raw_signals"

    source_id: Mapped[str] = _source_fk()
    company: Mapped[str | None] = mapped_column(Text)
    signal_type: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    period: Mapped[str | None] = mapped_column(Text)
    value: Mapped[_Num | None] = mapped_column(Numeric)


# Map raw_table name -> model, for the pipeline / drill chain to resolve dynamically.
RAW_TABLE_MODELS: dict[str, type[_RawBase]] = {
    m.__tablename__: m
    for m in (
        RawTradeFlow, RawRegulatory, RawFiling, RawTranscript, RawShipment,
        RawExternalMetric, RawIndustryReport, RawPatent, RawProcurement,
        RawStandard, RawNews, RawSignal,
    )
}


# =========================================================================== #
# LAYER 2 — CELLS
# =========================================================================== #
class Cell(Base):
    __tablename__ = "cells"
    __table_args__ = (
        UniqueConstraint("subcategory_id", "geography_id", "year"),
        CheckConstraint("confidence IN ('high','medium','low')", name="cells_confidence_check"),
    )

    cell_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    subcategory_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("taxonomy_subcategories.subcategory_id"), nullable=False
    )
    geography_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("geographies.geography_id"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    tam_revenue_usd_m: Mapped[_Num | None] = mapped_column(Numeric(14, 2))
    tam_low_usd_m: Mapped[_Num | None] = mapped_column(Numeric(14, 2))
    tam_high_usd_m: Mapped[_Num | None] = mapped_column(Numeric(14, 2))
    tam_units: Mapped[int | None] = mapped_column(BigInteger)
    confidence: Mapped[str | None] = mapped_column(Text)  # computed by the view, never hand-set
    confidence_rationale: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text, default="active")
    created_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))

    subcategory: Mapped["TaxonomySubcategory"] = relationship()
    geography: Mapped["Geography"] = relationship()
    triangulations: Mapped[list["CellTriangulation"]] = relationship(
        back_populates="cell", order_by="CellTriangulation.method_code"
    )


class CellTriangulation(Base):
    """One estimate per (method, source) for a cell. No row without a source_id."""

    __tablename__ = "cell_triangulation"
    __table_args__ = (
        UniqueConstraint("cell_id", "method_code", "source_id"),  # idempotent upsert key
    )

    triangulation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    cell_id: Mapped[int] = mapped_column(Integer, ForeignKey("cells.cell_id"), nullable=False)
    method_code: Mapped[str] = mapped_column(
        Text, ForeignKey("method_registry.method_code"), nullable=False
    )
    estimate_usd_m: Mapped[_Num] = mapped_column(Numeric(14, 2), nullable=False)
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sources.source_id"), nullable=False
    )  # invariant: every fact carries a source
    notes: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))

    cell: Mapped["Cell"] = relationship(back_populates="triangulations")
    method: Mapped["MethodRegistry"] = relationship()
    source: Mapped["Source"] = relationship()


class CellTriangulationSummary(Base):
    """Read-only mapping of the ``cell_triangulation_summary`` materialised view.

    Confidence is computed **here only** (Q5): ``COUNT(DISTINCT method_code)`` and
    thresholds from the ACTIVE validation profile. Never written from application
    code — the pipeline ``REFRESH``es the view and projects its verdict onto
    ``cells``. ``cell_id`` is the view's unique index, used here as the primary key
    so SQLAlchemy can map it.
    """

    __tablename__ = "cell_triangulation_summary"

    cell_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    n_estimates: Mapped[int | None] = mapped_column(Integer)
    n_distinct_methods: Mapped[int | None] = mapped_column(Integer)
    n_independent_signals: Mapped[int | None] = mapped_column(Integer)
    n_source_classes: Mapped[int | None] = mapped_column(Integer)
    estimate_min: Mapped[_Num | None] = mapped_column(Numeric)
    estimate_median: Mapped[_Num | None] = mapped_column(Numeric)
    estimate_max: Mapped[_Num | None] = mapped_column(Numeric)
    spread_ratio: Mapped[_Num | None] = mapped_column(Numeric)
    has_tier_a: Mapped[bool | None] = mapped_column(Boolean)
    effective_signals: Mapped[int | None] = mapped_column(Integer)
    qualifies_high: Mapped[bool | None] = mapped_column(Boolean)
    qualifies_medium: Mapped[bool | None] = mapped_column(Boolean)


# =========================================================================== #
# LAYER 3 — PLAYERS
# =========================================================================== #
class PlayerShare(Base):
    __tablename__ = "player_shares"
    __table_args__ = (UniqueConstraint("cell_id", "company_id", "player_role"),)

    share_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    cell_id: Mapped[int] = mapped_column(Integer, ForeignKey("cells.cell_id"), nullable=False)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.company_id"), nullable=False
    )
    player_role: Mapped[str] = mapped_column(Text, nullable=False)  # producer | distributor | OEM | CDMO ...
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    share_pct: Mapped[_Num | None] = mapped_column(Numeric(5, 2))
    share_low_pct: Mapped[_Num | None] = mapped_column(Numeric(5, 2))
    share_high_pct: Mapped[_Num | None] = mapped_column(Numeric(5, 2))
    revenue_usd_m: Mapped[_Num | None] = mapped_column(Numeric(14, 2))
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"), nullable=False)
    confidence: Mapped[str | None] = mapped_column(Text)

    company: Mapped["Company"] = relationship()
    source: Mapped["Source"] = relationship()


class SupplierRelationship(Base):
    __tablename__ = "supplier_relationships"

    relationship_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    buyer_id: Mapped[int] = mapped_column(Integer, ForeignKey("companies.company_id"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(Integer, ForeignKey("companies.company_id"), nullable=False)
    cell_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("cells.cell_id"))
    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_type: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_strength: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    buyer: Mapped["Company"] = relationship(foreign_keys=[buyer_id])
    supplier: Mapped["Company"] = relationship(foreign_keys=[supplier_id])


class Facility(Base):
    __tablename__ = "facilities"

    facility_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[int] = mapped_column(Integer, ForeignKey("companies.company_id"), nullable=False)
    country: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    facility_type: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"), nullable=False)

    company: Mapped["Company"] = relationship()


# =========================================================================== #
# LAYER 4 — DECISIONS
# =========================================================================== #
class Catalyst(Base):
    __tablename__ = "catalysts"
    __table_args__ = (
        CheckConstraint(
            "impact_direction IN ('positive','negative')",
            name="catalysts_impact_direction_check",
        ),
    )

    catalyst_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    cell_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("cells.cell_id"))
    company_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("companies.company_id"))
    catalyst_type: Mapped[str] = mapped_column(Text, nullable=False)
    impact_direction: Mapped[str] = mapped_column(Text, nullable=False)  # positive | negative
    expected_quarter: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"), nullable=False)


class Recommendation(Base):
    __tablename__ = "recommendations"

    recommendation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    priority_score: Mapped[_Num | None] = mapped_column(Numeric(5, 2))
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    derivation_assumption_ids: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))


class CellAssumptionLink(Base):
    __tablename__ = "cell_assumption_link"

    cell_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cells.cell_id"), primary_key=True
    )
    assumption_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("assumptions.assumption_id"), primary_key=True
    )
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[_Num | None] = mapped_column(Numeric(3, 2), default=1.0)


class Commentary(Base):
    __tablename__ = "commentary"

    commentary_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engagement_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)  # cell | subcategory | family | engagement
    scope_id: Mapped[int | None] = mapped_column(Integer)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[str | None] = mapped_column(Text, default="all")  # all | analyst | business | external
    author: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_TS | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "Base",
    "Engagement",
    # spine
    "TaxonomyFamily", "TaxonomySubcategory", "Geography", "Company", "Source",
    "MethodRegistry", "ConnectorCredential", "CredentialAudit",
    "ValidationProfile", "Assumption",
    # raw
    "RawTradeFlow", "RawRegulatory", "RawFiling", "RawTranscript", "RawShipment",
    "RawExternalMetric", "RawIndustryReport", "RawPatent", "RawProcurement",
    "RawStandard", "RawNews", "RawSignal", "RAW_TABLE_MODELS",
    # cells
    "Cell", "CellTriangulation", "CellTriangulationSummary",
    # players
    "PlayerShare", "SupplierRelationship", "Facility",
    # decisions
    "Catalyst", "Recommendation", "CellAssumptionLink", "Commentary",
]
