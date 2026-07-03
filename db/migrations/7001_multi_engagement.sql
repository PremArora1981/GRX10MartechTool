-- Multi-engagement migration (Phase 1). Idempotent — safe to re-run.
-- Adds the `engagements` spine, backfills all existing rows to the protected
-- Medtech demo engagement, scopes every per-engagement table with engagement_id,
-- rebuilds the affected unique constraints, and redefines the confidence matview
-- to resolve the active validation profile PER ENGAGEMENT.
--
-- Apply:  docker exec -i grx10-mr-db psql "$DATABASE_URL" -v ON_ERROR_STOP=1 < db/migrations/7001_multi_engagement.sql
-- (Render: allow-list host IP, apply via psql, remove allow-list — see docs.)

BEGIN;

-- 1. Engagements spine ------------------------------------------------------
CREATE TABLE IF NOT EXISTS engagements (
  engagement_id      text PRIMARY KEY,
  name               text NOT NULL,
  is_demo            boolean NOT NULL DEFAULT false,
  status             text NOT NULL DEFAULT 'active',   -- active | archived
  active_profile     text NOT NULL DEFAULT 'Standard',
  web_search_enabled boolean NOT NULL DEFAULT true,
  brief_text         text,
  created_at         timestamptz NOT NULL DEFAULT now()
);

INSERT INTO engagements (engagement_id, name, is_demo, status, active_profile, brief_text)
VALUES ('eng_medtech', 'Medtech APAC', true, 'active', 'Standard',
        'Jabil Medtech APAC reference engagement (China, Malaysia, Singapore).')
ON CONFLICT (engagement_id) DO NOTHING;

-- 2. Scope every per-engagement table: add engagement_id, backfill, NOT NULL,
--    FK, and a filter index. method_registry / validation_profiles stay global.
DO $$
DECLARE
  t text;
  scoped text[] := ARRAY[
    'taxonomy_families','taxonomy_subcategories','geographies','sources','companies',
    'cells','cell_triangulation','player_shares','supplier_relationships','facilities',
    'catalysts','recommendations','commentary','assumptions','cell_assumption_link',
    'raw_trade_flows','raw_regulatory','raw_filings','raw_transcripts','raw_shipments',
    'raw_external_metrics','raw_industry_reports','raw_patents','raw_procurement',
    'raw_standards','raw_news','raw_signals'
  ];
BEGIN
  FOREACH t IN ARRAY scoped LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS engagement_id text', t);
    EXECUTE format('UPDATE %I SET engagement_id = ''eng_medtech'' WHERE engagement_id IS NULL', t);
    EXECUTE format('ALTER TABLE %I ALTER COLUMN engagement_id SET NOT NULL', t);
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = t || '_engagement_fk') THEN
      EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (engagement_id) REFERENCES engagements(engagement_id)',
        t, t || '_engagement_fk');
    END IF;
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (engagement_id)',
                   'idx_' || t || '_engagement', t);
  END LOOP;
END $$;

-- 3. Rebuild unique constraints that must now be engagement-scoped -----------
ALTER TABLE cells DROP CONSTRAINT IF EXISTS cells_subcategory_id_geography_id_year_key;
ALTER TABLE cells ADD CONSTRAINT cells_eng_sub_geo_year_key
  UNIQUE (engagement_id, subcategory_id, geography_id, year);

ALTER TABLE geographies DROP CONSTRAINT IF EXISTS geographies_country_segment_key;
ALTER TABLE geographies ADD CONSTRAINT geographies_eng_country_segment_key
  UNIQUE (engagement_id, country, segment);
-- cell_triangulation / player_shares uniques key off cell_id (already engagement-implied).

-- 4. Redefine the confidence matview: engagement-carried, per-engagement active
--    profile (was a single global WHERE is_active row).
DROP MATERIALIZED VIEW IF EXISTS cell_triangulation_summary;
CREATE MATERIALIZED VIEW cell_triangulation_summary AS
WITH sig AS (
  SELECT
    ct.cell_id,
    c.engagement_id,
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
  JOIN cells c            ON c.cell_id = ct.cell_id
  JOIN method_registry mr ON mr.method_code = ct.method_code
  GROUP BY ct.cell_id, c.engagement_id
)
SELECT
  sig.*,
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
FROM sig
JOIN engagements e         ON e.engagement_id = sig.engagement_id
JOIN validation_profiles a ON a.name = e.active_profile;

CREATE UNIQUE INDEX ON cell_triangulation_summary (cell_id);
CREATE INDEX ON cell_triangulation_summary (engagement_id);

COMMIT;
