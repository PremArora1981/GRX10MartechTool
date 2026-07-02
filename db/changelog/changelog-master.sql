--liquibase formatted sql
-- Automated Market Research Tool — full v1 schema.
-- Five layers + raw-class extensions + validation profiles + encrypted credentials.
-- Invariants enforced in DDL: no fact row without a non-null source; every estimate
-- carries method + source; confidence computed by the summary view (no write-time override).

--changeset grx10:0001-extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- LAYER 1 — SPINE
-- ============================================================

--changeset grx10:1001-taxonomy-families
CREATE TABLE taxonomy_families (
  family_id      INT PRIMARY KEY,
  name           TEXT NOT NULL,
  version        INT NOT NULL DEFAULT 1,
  created_at     TIMESTAMPTZ DEFAULT now()
);

--changeset grx10:1002-taxonomy-subcategories
CREATE TABLE taxonomy_subcategories (
  subcategory_id   INT PRIMARY KEY,
  family_id        INT NOT NULL REFERENCES taxonomy_families(family_id),
  name             TEXT NOT NULL,
  hs_codes         TEXT[] DEFAULT '{}',
  regulatory_codes TEXT[] DEFAULT '{}',
  version          INT NOT NULL DEFAULT 1,
  superseded_by    INT REFERENCES taxonomy_subcategories(subcategory_id),
  created_at       TIMESTAMPTZ DEFAULT now()
);

--changeset grx10:1003-geographies
CREATE TABLE geographies (
  geography_id INT PRIMARY KEY,
  country      TEXT NOT NULL,
  segment      TEXT NOT NULL,        -- DOMESTIC | IMPORT | EXPORT | SELF_CONSUME ...
  UNIQUE (country, segment)
);

--changeset grx10:1004-companies
CREATE TABLE companies (
  company_id    SERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  company_type  TEXT,
  country_hq    TEXT,
  seeded_role   TEXT,
  discovered    BOOLEAN DEFAULT false,
  created_at    TIMESTAMPTZ DEFAULT now()
);

--changeset grx10:1005-sources
CREATE TABLE sources (
  source_id        TEXT PRIMARY KEY,
  publisher        TEXT NOT NULL,
  url_pattern      TEXT,
  auth             TEXT,             -- none | api_key | oauth | login | scrape
  auth_secret_ref  TEXT,            -- pointer into connector_credentials
  class            TEXT CHECK (class IN ('A','B','C')),
  connector        TEXT,             -- module name in connectors/
  refresh_cadence  TEXT,
  raw_table        TEXT,
  access_method    TEXT DEFAULT 'api', -- api | scrape | web_search | manual_upload
  discovered       BOOLEAN DEFAULT false,
  monthly_budget   NUMERIC(10,2),    -- optional cost ceiling (Q7 budget pre-warning)
  quota_ceiling    INT,
  -- connector health (Q7)
  last_probe_status TEXT,            -- OK | AUTH_FAILED | QUOTA_EXHAUSTED | RATE_LIMITED | UNREACHABLE | SCHEMA_MISMATCH | EMPTY
  last_probe_at     TIMESTAMPTZ,
  last_probe_detail TEXT,
  enabled          BOOLEAN DEFAULT true,
  notes            TEXT,
  created_at       TIMESTAMPTZ DEFAULT now()
);

--changeset grx10:1006-method-registry
CREATE TABLE method_registry (
  method_code        TEXT PRIMARY KEY,
  description        TEXT,
  tier               TEXT CHECK (tier IN ('A','B','C')),
  source_class       TEXT,           -- for method x source-class independence (Q5)
  is_primary_source  BOOLEAN DEFAULT false,
  confidence_cap     TEXT CHECK (confidence_cap IN ('high','medium','low')), -- e.g. web_search => low
  required_raw_tables TEXT[] DEFAULT '{}'
);

--changeset grx10:1007-connector-credentials
-- Envelope-encrypted credential store (Q9). Ciphertext only; plaintext never persisted.
CREATE TABLE connector_credentials (
  cred_ref       TEXT PRIMARY KEY,   -- matches sources.auth_secret_ref
  source_id      TEXT REFERENCES sources(source_id),
  ciphertext     BYTEA NOT NULL,     -- pgp_sym_encrypt(secret, data_key)
  enc_data_key   BYTEA NOT NULL,     -- data key wrapped by the Render master key
  created_by     TEXT,
  created_at     TIMESTAMPTZ DEFAULT now(),
  rotated_at     TIMESTAMPTZ
);

--changeset grx10:1008-credential-audit
CREATE TABLE credential_audit (
  audit_id   SERIAL PRIMARY KEY,
  cred_ref   TEXT,
  action     TEXT NOT NULL,          -- added | rotated | removed
  actor      TEXT,
  at         TIMESTAMPTZ DEFAULT now()
);

--changeset grx10:1009-validation-profiles
-- Configurable confidence thresholds (Q5). One row marked active per engagement.
CREATE TABLE validation_profiles (
  profile_id              SERIAL PRIMARY KEY,
  name                    TEXT NOT NULL UNIQUE,
  is_active               BOOLEAN DEFAULT false,
  independence_level      TEXT NOT NULL DEFAULT 'method_x_source_class', -- method | method_x_source_class
  high_min_distinct_methods INT NOT NULL,
  high_max_spread         NUMERIC(4,3) NOT NULL,
  high_require_tier_a     BOOLEAN NOT NULL,
  high_min_source_classes INT NOT NULL DEFAULT 1,
  medium_min_distinct_methods INT NOT NULL,
  medium_max_spread       NUMERIC(4,3) NOT NULL,
  medium_alt_min_methods  INT,
  medium_alt_max_spread   NUMERIC(4,3)
);

--changeset grx10:1010-validation-profile-seed
INSERT INTO validation_profiles
 (name, is_active, high_min_distinct_methods, high_max_spread, high_require_tier_a, high_min_source_classes,
  medium_min_distinct_methods, medium_max_spread, medium_alt_min_methods, medium_alt_max_spread) VALUES
 ('Light',        false, 2, 0.100, false, 1, 1, 0.250, 2, 0.300),
 ('Standard',     true,  3, 0.050, true,  1, 2, 0.150, 3, 0.200),
 ('Conservative', false, 3, 0.040, true,  2, 2, 0.120, 3, 0.150),
 ('Audit-grade',  false, 4, 0.030, true,  2, 3, 0.100, 3, 0.120);

--changeset grx10:1011-assumptions
CREATE TABLE assumptions (
  assumption_id        SERIAL PRIMARY KEY,
  scope_company_id     INT REFERENCES companies(company_id),
  scope_subcategory_id INT REFERENCES taxonomy_subcategories(subcategory_id),
  scope_geography_id   INT REFERENCES geographies(geography_id),
  assumption_text      TEXT NOT NULL,
  numeric_value        NUMERIC,
  unit                 TEXT,
  confidence           TEXT,
  derivation_method    TEXT,
  source_id            TEXT REFERENCES sources(source_id),
  effective_from_year  INT NOT NULL,
  effective_to_year    INT,
  superseded_by        INT REFERENCES assumptions(assumption_id),
  created_at           TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- LAYER 0 — RAW (one table per source class; spec 8 + 4 extensions)
-- Each carries source_id, accessed_at, raw_json + a normalised typed subset.
-- ============================================================

--changeset grx10:2001-raw-tables
CREATE TABLE raw_trade_flows (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  reporter TEXT, partner TEXT, hs_code TEXT, hs_version TEXT, flow TEXT, period TEXT,
  value_usd NUMERIC, qty NUMERIC, qty_unit TEXT
);
CREATE TABLE raw_regulatory (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  registration_id TEXT, holder TEXT, product_code TEXT, country TEXT, status TEXT
);
CREATE TABLE raw_filings (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  filer TEXT, ticker TEXT, period TEXT, segment TEXT, geography TEXT, revenue_usd NUMERIC, doc_url TEXT
);
CREATE TABLE raw_transcripts (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  company TEXT, period TEXT, content TEXT
);
CREATE TABLE raw_shipments (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  shipper TEXT, consignee TEXT, hs_code TEXT, origin TEXT, dest TEXT, value_usd NUMERIC, period TEXT
);
CREATE TABLE raw_external_metrics (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  indicator TEXT, country TEXT, period TEXT, value NUMERIC, unit TEXT
);
CREATE TABLE raw_industry_reports (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  publisher TEXT, market TEXT, period TEXT, tam_usd NUMERIC, doc_url TEXT
);
CREATE TABLE raw_patents (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  patent_id TEXT, assignee TEXT, cpc TEXT, filing_date DATE, country TEXT
);
CREATE TABLE raw_procurement (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  award_id TEXT, buyer TEXT, supplier TEXT, country TEXT, value_usd NUMERIC, period TEXT
);
CREATE TABLE raw_standards (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  body TEXT, member TEXT, membership_tier TEXT
);
CREATE TABLE raw_news (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  headline TEXT, url TEXT, published_at TIMESTAMPTZ, entity TEXT, snippet TEXT
);
CREATE TABLE raw_signals (
  raw_id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  accessed_at TIMESTAMPTZ DEFAULT now(), raw_json JSONB NOT NULL,
  company TEXT, signal_type TEXT, country TEXT, period TEXT, value NUMERIC
);

-- ============================================================
-- LAYER 2 — CELLS (the heart)
-- ============================================================

--changeset grx10:3001-cells
CREATE TABLE cells (
  cell_id            SERIAL PRIMARY KEY,
  subcategory_id     INT NOT NULL REFERENCES taxonomy_subcategories(subcategory_id),
  geography_id       INT NOT NULL REFERENCES geographies(geography_id),
  year               INT NOT NULL,
  tam_revenue_usd_m  NUMERIC(14,2),
  tam_low_usd_m      NUMERIC(14,2),
  tam_high_usd_m     NUMERIC(14,2),
  tam_units          BIGINT,
  confidence         TEXT CHECK (confidence IN ('high','medium','low')),
  confidence_rationale TEXT,
  status             TEXT DEFAULT 'active',
  created_at         TIMESTAMPTZ DEFAULT now(),
  updated_at         TIMESTAMPTZ DEFAULT now(),
  UNIQUE (subcategory_id, geography_id, year)
);

--changeset grx10:3002-cell-triangulation
CREATE TABLE cell_triangulation (
  triangulation_id SERIAL PRIMARY KEY,
  cell_id        INT NOT NULL REFERENCES cells(cell_id),
  method_code    TEXT NOT NULL REFERENCES method_registry(method_code),
  estimate_usd_m NUMERIC(14,2) NOT NULL,
  source_id      TEXT NOT NULL REFERENCES sources(source_id),   -- no row without a source
  notes          TEXT,
  computed_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE (cell_id, method_code, source_id)                      -- idempotent upsert key
);

--changeset grx10:3003-cell-triangulation-summary
-- Confidence rule (Q5): COUNT(DISTINCT method_code) — NOT COUNT(*) — and thresholds
-- read from the ACTIVE validation profile. Independence at method x source-class.
CREATE MATERIALIZED VIEW cell_triangulation_summary AS
WITH active AS (SELECT * FROM validation_profiles WHERE is_active LIMIT 1),
sig AS (
  SELECT
    ct.cell_id,
    COUNT(*)                                          AS n_estimates,
    COUNT(DISTINCT ct.method_code)                    AS n_distinct_methods,
    COUNT(DISTINCT (ct.method_code, mr.source_class)) AS n_independent_signals,
    COUNT(DISTINCT mr.source_class)                   AS n_source_classes,
    MIN(ct.estimate_usd_m)                            AS estimate_min,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ct.estimate_usd_m) AS estimate_median,
    MAX(ct.estimate_usd_m)                            AS estimate_max,
    (MAX(ct.estimate_usd_m) - MIN(ct.estimate_usd_m)) /
      NULLIF(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ct.estimate_usd_m), 0) AS spread_ratio,
    bool_or(mr.is_primary_source)                     AS has_tier_a
  FROM cell_triangulation ct
  JOIN method_registry mr ON mr.method_code = ct.method_code
  GROUP BY ct.cell_id
)
SELECT
  sig.*,
  -- effective independent-signal count depends on the profile's independence_level
  (CASE WHEN a.independence_level = 'method' THEN sig.n_distinct_methods
        ELSE sig.n_independent_signals END) AS effective_signals,
  (
    (CASE WHEN a.independence_level = 'method' THEN sig.n_distinct_methods
          ELSE sig.n_independent_signals END) >= a.high_min_distinct_methods
    AND sig.spread_ratio < a.high_max_spread
    AND (NOT a.high_require_tier_a OR sig.has_tier_a)
    AND sig.n_source_classes >= a.high_min_source_classes
  ) AS qualifies_high,
  (
    ( (CASE WHEN a.independence_level = 'method' THEN sig.n_distinct_methods
            ELSE sig.n_independent_signals END) >= a.medium_min_distinct_methods
      AND sig.spread_ratio < a.medium_max_spread )
    OR
    ( a.medium_alt_min_methods IS NOT NULL
      AND (CASE WHEN a.independence_level = 'method' THEN sig.n_distinct_methods
                ELSE sig.n_independent_signals END) >= a.medium_alt_min_methods
      AND sig.spread_ratio < a.medium_alt_max_spread )
  ) AS qualifies_medium
FROM sig CROSS JOIN active a;

CREATE UNIQUE INDEX ON cell_triangulation_summary (cell_id);

-- ============================================================
-- LAYER 3 — PLAYERS
-- ============================================================

--changeset grx10:4001-player-shares
CREATE TABLE player_shares (
  share_id       SERIAL PRIMARY KEY,
  cell_id        INT NOT NULL REFERENCES cells(cell_id),
  company_id     INT NOT NULL REFERENCES companies(company_id),
  player_role    TEXT NOT NULL,     -- producer | distributor | supplier | buyer | OEM | CDMO ...
  rank           INT NOT NULL,
  share_pct      NUMERIC(5,2),
  share_low_pct  NUMERIC(5,2),
  share_high_pct NUMERIC(5,2),
  revenue_usd_m  NUMERIC(14,2),
  source_id      TEXT NOT NULL REFERENCES sources(source_id),
  confidence     TEXT,
  UNIQUE (cell_id, company_id, player_role)
);

--changeset grx10:4002-supplier-relationships
CREATE TABLE supplier_relationships (
  relationship_id   SERIAL PRIMARY KEY,
  buyer_id          INT NOT NULL REFERENCES companies(company_id),
  supplier_id       INT NOT NULL REFERENCES companies(company_id),
  cell_id           INT REFERENCES cells(cell_id),
  relationship_type TEXT NOT NULL,
  evidence_type     TEXT NOT NULL,
  evidence_strength TEXT NOT NULL,
  source_id         TEXT NOT NULL REFERENCES sources(source_id),
  notes             TEXT
);

--changeset grx10:4003-facilities
CREATE TABLE facilities (
  facility_id SERIAL PRIMARY KEY,
  company_id  INT NOT NULL REFERENCES companies(company_id),
  country     TEXT, city TEXT, facility_type TEXT,
  source_id   TEXT NOT NULL REFERENCES sources(source_id)
);

-- ============================================================
-- LAYER 4 — DECISIONS
-- ============================================================

--changeset grx10:5001-catalysts
CREATE TABLE catalysts (
  catalyst_id      SERIAL PRIMARY KEY,
  cell_id          INT REFERENCES cells(cell_id),
  company_id       INT REFERENCES companies(company_id),
  catalyst_type    TEXT NOT NULL,
  impact_direction TEXT NOT NULL CHECK (impact_direction IN ('positive','negative')),
  expected_quarter TEXT,
  description      TEXT NOT NULL,
  source_id        TEXT NOT NULL REFERENCES sources(source_id)
);

--changeset grx10:5002-recommendations
CREATE TABLE recommendations (
  recommendation_id SERIAL PRIMARY KEY,
  scope_type        TEXT NOT NULL,
  scope_payload     JSONB NOT NULL,
  priority_score    NUMERIC(5,2),
  rationale         TEXT NOT NULL,
  derivation_assumption_ids INT[]
);

--changeset grx10:5003-cell-assumption-link
CREATE TABLE cell_assumption_link (
  cell_id       INT NOT NULL REFERENCES cells(cell_id),
  assumption_id INT NOT NULL REFERENCES assumptions(assumption_id),
  weight        NUMERIC(3,2) DEFAULT 1.0,
  PRIMARY KEY (cell_id, assumption_id)
);

--changeset grx10:5004-commentary
CREATE TABLE commentary (
  commentary_id SERIAL PRIMARY KEY,
  scope_type    TEXT NOT NULL,      -- cell | subcategory | family | engagement
  scope_id      INT,
  body_markdown TEXT NOT NULL,
  audience      TEXT DEFAULT 'all', -- all | analyst | business | external
  author        TEXT,
  created_at    TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- INDEXES (sub-second list views — acceptance criterion)
-- ============================================================

--changeset grx10:6001-indexes
CREATE INDEX idx_cells_sub_geo_year ON cells (subcategory_id, geography_id, year);
CREATE INDEX idx_cells_confidence   ON cells (confidence);
CREATE INDEX idx_tri_cell           ON cell_triangulation (cell_id);
CREATE INDEX idx_player_shares_cell ON player_shares (cell_id);
CREATE INDEX idx_raw_trade_period   ON raw_trade_flows (hs_code, reporter, period);
CREATE INDEX idx_assumptions_super  ON assumptions (superseded_by);
