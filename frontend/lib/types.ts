/**
 * Domain types for the GRX10 Automated Market Research Tool frontend.
 *
 * These mirror the database schema in `db/changelog/changelog-master.sql` and
 * the FastAPI Pydantic response models. They are the contract every screen
 * agent codes against, so keep field names aligned with the API payloads.
 */

// ---------------------------------------------------------------------------
// Enums / unions (kept in sync with DB CHECK constraints + the spec)
// ---------------------------------------------------------------------------

/** Programmatically-computed confidence (never set by a human at write time). */
export type Confidence = "high" | "medium" | "low";

/** Source confidence class — A primary structured, B procedural, C support. */
export type SourceClass = "A" | "B" | "C";

/** Method tier (mirrors source class semantics for triangulation). */
export type MethodTier = "A" | "B" | "C";

/** 7-state connector-health taxonomy (Q7). Matches `ProbeStatus` in connectors/base.py. */
export type ProbeStatus =
  | "OK"
  | "AUTH_FAILED"
  | "QUOTA_EXHAUSTED"
  | "RATE_LIMITED"
  | "UNREACHABLE"
  | "SCHEMA_MISMATCH"
  | "EMPTY";

/** Trade-direction / segment dimension (geographies.segment). Open-ended by design. */
export type Segment =
  | "DOMESTIC"
  | "IMPORT"
  | "EXPORT"
  | "SELF_CONSUME"
  | (string & {});

/** Application roles mapped from WorkOS/IdP claims (Q10). */
export type AppRole = "owner_admin" | "analyst" | "business" | "external";

// ---------------------------------------------------------------------------
// Layer 1 — spine
// ---------------------------------------------------------------------------

export interface Geography {
  geography_id: number;
  country: string;
  segment: Segment;
}

export interface TaxonomyFamily {
  family_id: number;
  name: string;
  version: number;
}

export interface TaxonomySubcategory {
  subcategory_id: number;
  family_id: number;
  name: string;
  hs_codes: string[];
  regulatory_codes: string[];
  version: number;
  superseded_by: number | null;
}

export interface Company {
  company_id: number;
  name: string;
  company_type: string | null;
  country_hq: string | null;
  seeded_role: string | null;
  discovered: boolean;
}

export interface Source {
  source_id: string;
  publisher: string;
  url_pattern: string | null;
  auth: string | null;
  /** Pointer to the encrypted credential row; null means no credential stored yet. */
  auth_secret_ref: string | null;
  class: SourceClass | null;
  connector: string | null;
  refresh_cadence: string | null;
  raw_table: string | null;
  access_method: string | null;
  discovered: boolean;
  monthly_budget: number | null;
  quota_ceiling: number | null;
  last_probe_status: ProbeStatus | null;
  last_probe_at: string | null;
  last_probe_detail: string | null;
  enabled: boolean;
  notes: string | null;
  /**
   * Computed by the backend: true when the connector is at >=80% of
   * monthly_budget / quota_ceiling (Q7 budget pre-warning).
   */
  budget_warning?: boolean;
}

// ---------------------------------------------------------------------------
// Connector admin — custom REST / scraping source creation (Track B)
// ---------------------------------------------------------------------------

/** A single field mapping suggested by the AI-assist endpoint. */
export interface FieldMapping {
  /** Dot-notation JSON path in the raw API response (e.g. "data[].value"). */
  raw_field: string;
  /** Target column in the destination raw_* table. */
  mapped_column: string;
  /** Optional transform hint (e.g. "parse_iso_date", "multiply_by_1e-6"). */
  transform: string | null;
  /** AI-generated rationale for this mapping. */
  notes: string | null;
}

/** Request body for the AI-assisted field-mapping endpoint. */
export interface SuggestMappingRequest {
  url_pattern: string;
  raw_table: string;
  /** Optional: paste a sample JSON response to improve mapping accuracy. */
  sample_response: string | null;
}

/** Response from POST /connectors/suggest-mapping. */
export interface SuggestMappingResponse {
  mappings: FieldMapping[];
  /** AI confidence in the suggestions. */
  confidence: "high" | "medium" | "low";
  notes: string | null;
}

/** Payload for POST /connectors (create a new source row). */
export interface AddSourcePayload {
  source_id: string;
  publisher: string;
  url_pattern: string | null;
  auth: string;
  class: SourceClass;
  connector: string | null;
  refresh_cadence: string | null;
  raw_table: string | null;
  access_method: string;
  monthly_budget: number | null;
  quota_ceiling: number | null;
  notes: string | null;
  /** Field mappings stored by the backend alongside the source row. */
  field_mappings: FieldMapping[];
}

/** Response from POST /connectors/:id/credential. */
export interface SetCredentialResponse {
  cred_ref: string;
  rotated_at: string;
}

export interface MethodRegistryEntry {
  method_code: string;
  description: string | null;
  tier: MethodTier | null;
  source_class: string | null;
  is_primary_source: boolean;
  confidence_cap: Confidence | null;
  required_raw_tables: string[];
}

export interface ValidationProfile {
  profile_id: number;
  name: string;
  is_active: boolean;
  independence_level: "method" | "method_x_source_class";
  high_min_distinct_methods: number;
  high_max_spread: number;
  high_require_tier_a: boolean;
  high_min_source_classes: number;
  medium_min_distinct_methods: number;
  medium_max_spread: number;
  medium_alt_min_methods: number | null;
  medium_alt_max_spread: number | null;
}

export interface Assumption {
  assumption_id: number;
  scope_company_id: number | null;
  scope_subcategory_id: number | null;
  scope_geography_id: number | null;
  assumption_text: string;
  numeric_value: number | null;
  unit: string | null;
  confidence: string | null;
  derivation_method: string | null;
  source_id: string | null;
  effective_from_year: number;
  effective_to_year: number | null;
  superseded_by: number | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Sources view (W2) — /sources endpoint types
// ---------------------------------------------------------------------------

/**
 * One source row from GET /sources, enriched with:
 * - why: plain-English rationale (from notes or class default)
 * - used_for: method_codes whose required_raw_tables include this source's raw_table
 */
export interface SourceDetail {
  source_id: string;
  publisher: string;
  source_class: SourceClass | null;
  access_method: string | null;
  raw_table: string | null;
  enabled: boolean | null;
  last_probe_status: ProbeStatus | null;
  last_probe_detail: string | null;
  notes: string | null;
  why: string;
  used_for: string[];
}

/** Response from GET /sources/recommended — method_code → [source_id] map. */
export interface RecommendedSources {
  method_map: Record<string, string[]>;
}

// ---------------------------------------------------------------------------
// Layer 2 — cells
// ---------------------------------------------------------------------------

export interface Cell {
  cell_id: number;
  subcategory_id: number;
  geography_id: number;
  year: number;
  tam_revenue_usd_m: number | null;
  tam_low_usd_m: number | null;
  tam_high_usd_m: number | null;
  tam_units: number | null;
  confidence: Confidence | null;
  confidence_rationale: string | null;
  status: string;
  updated_at: string;
}

/** A cell joined with its denormalised display labels (for list views). */
export interface CellView extends Cell {
  subcategory_name: string;
  /** Taxonomy family name — populated when the backend joins taxonomy_families.
   *  Currently absent from CellSummary; treat as optional. */
  family_name?: string;
  country: string;
  segment: Segment;
  /** Distinct methods that triangulated this cell (drives the confidence chip).
   *  Currently absent from CellSummary; treat as optional. */
  n_distinct_methods?: number;
}

export interface CellTriangulation {
  triangulation_id: number;
  cell_id: number;
  method_code: string;
  estimate_usd_m: number;
  source_id: string;
  notes: string | null;
  computed_at: string;
}

/** A triangulation row enriched with its source + method labels for the audit chain. */
export interface CellTriangulationView extends CellTriangulation {
  method_description: string | null;
  method_tier: MethodTier | null;
  source_publisher: string;
  source_url: string | null;
  source_class: SourceClass | null;
  source_access_method: string | null;
}

export interface CellTriangulationSummary {
  cell_id: number;
  n_estimates: number;
  n_distinct_methods: number;
  n_independent_signals: number;
  n_source_classes: number;
  estimate_min: number | null;
  estimate_median: number | null;
  estimate_max: number | null;
  spread_ratio: number | null;
  has_tier_a: boolean;
  effective_signals: number;
  qualifies_high: boolean;
  qualifies_medium: boolean;
}

// ---------------------------------------------------------------------------
// Layer 3 — players
// ---------------------------------------------------------------------------

export interface PlayerShare {
  share_id: number;
  cell_id: number;
  company_id: number;
  company_name: string;
  player_role: string;
  rank: number;
  share_pct: number | null;
  share_low_pct: number | null;
  share_high_pct: number | null;
  revenue_usd_m: number | null;
  source_id: string;
  confidence: string | null;
}

export interface SupplierRelationship {
  relationship_id: number;
  buyer_id: number;
  buyer_name: string;
  supplier_id: number;
  supplier_name: string;
  cell_id: number | null;
  relationship_type: string;
  evidence_type: string;
  evidence_strength: string;
  source_id: string;
}

// ---------------------------------------------------------------------------
// Status page — frontend canonical types (kept for backward compat)
// ---------------------------------------------------------------------------

export interface SourceFreshness {
  source_id: string;
  publisher: string;
  last_probe_status: ProbeStatus | null;
  last_probe_at: string | null;
  last_probe_detail: string | null;
  raw_row_count: number;
  latest_raw_at: string | null;
  enabled: boolean;
  /** true once the connector is at >=80% of monthly_budget / quota_ceiling (Q7). */
  budget_warning: boolean;
}

export interface CoverageSummary {
  total_cells: number;
  high_confidence: number;
  medium_confidence: number;
  low_confidence: number;
  unsized_cells: number;
}

export interface StatusSnapshot {
  generated_at: string;
  sources: SourceFreshness[];
  coverage: CoverageSummary;
  active_profile: string;
}

// ---------------------------------------------------------------------------
// Status page — types that accurately mirror the actual backend response shapes
// (backend/app/routers/status.py).  Use these in the status screen.
// ---------------------------------------------------------------------------

/**
 * Aggregate count of *enabled* sources in each 7-state probe-status bucket.
 * Never-probed covers enabled sources with a NULL last_probe_status.
 * Mirrors `ConnectorHealthSummary` in the backend.
 */
export interface ConnectorHealthAggregate {
  ok: number;
  auth_failed: number;
  quota_exhausted: number;
  rate_limited: number;
  unreachable: number;
  schema_mismatch: number;
  empty: number;
  never_probed: number;
  total_enabled: number;
  total_disabled: number;
}

/**
 * Cell-coverage stats from the backend.  `cells_by_confidence` has keys
 * "high" | "medium" | "low" | "none" mapped to row counts.
 * Mirrors `CellCoverageStats` in the backend.
 */
export interface BackendCellCoverage {
  total_cells: number;
  cells_with_estimates: number;
  coverage_pct: number;
  cells_by_confidence: Record<string, number>;
}

/**
 * Full pipeline health snapshot returned by GET /status.
 * Mirrors `StatusResponse` in the backend.
 */
export interface BackendStatusSnapshot {
  pipeline_healthy: boolean;
  last_pipeline_ok_at: string | null;
  connector_health: ConnectorHealthAggregate;
  cell_coverage: BackendCellCoverage;
  generated_at: string;
}

/**
 * Per-source freshness row returned by GET /status/sources.
 * Mirrors `SourceFreshnessItem` in the backend.
 * Note: `raw_row_count` and `latest_raw_at` are not yet implemented server-side
 * and will be absent; backend adds `is_stale`, `monthly_budget`, `quota_ceiling`.
 */
export interface BackendSourceFreshness {
  source_id: string;
  publisher: string;
  access_method: string | null;
  source_class: SourceClass | null;
  last_probe_status: ProbeStatus | null;
  last_probe_at: string | null;
  last_probe_detail: string | null;
  enabled: boolean;
  /** Server-computed: true when last_probe_at is older than the freshness window. */
  is_stale: boolean;
  /** Q7 pre-warning: source has a cost ceiling AND last probe returned QUOTA_EXHAUSTED. */
  budget_warning: boolean;
  monthly_budget: number | null;
  quota_ceiling: number | null;
}

// ---------------------------------------------------------------------------
// Shared list/query shapes
// ---------------------------------------------------------------------------

export interface Paginated<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface CellQuery {
  subcategory_id?: number;
  geography_id?: number;
  family_id?: number;
  year?: number;
  confidence?: Confidence;
  segment?: Segment;
  limit?: number;
  offset?: number;
}

// ---------------------------------------------------------------------------
// NL Brief interpreter (W3) — /brief/interpret types
// ---------------------------------------------------------------------------

/** One source recommended by the brief interpreter, with rationale. */
export interface BriefRecommendedSource {
  source_id: string;
  publisher: string;
  source_class: string;
  why: string;
}

/** One connector in the brief's execution blueprint. */
export interface BriefConnectorPlanItem {
  source_id: string;
  publisher: string;
  source_class: string;
  raw_table: string;
  access: string;
  status: string;
  pulls: string;
  parsing: string;
  base_url?: string;
  endpoint_path?: string;
  auth_type?: string;
}

/** One proposed subcategory (new-vertical taxonomy) from the brief. */
export interface BriefProposedSubcategory {
  family: string;
  name: string;
  hs_codes: string[];
  regulatory_codes: string[];
}

// ---------------------------------------------------------------------------
// Engagements (multi-engagement)
// ---------------------------------------------------------------------------

export interface Engagement {
  engagement_id: string;
  name: string;
  is_demo: boolean;
  status: string; // active | archived
  active_profile: string;
  web_search_enabled: boolean;
  brief_text: string | null;
  created_at: string | null;
}

/** Request body to create an engagement from a confirmed brief plan. */
export interface EngagementCreate {
  name: string;
  brief_text?: string | null;
  families?: string[];
  geographies?: string[];
  year_from: number;
  year_to: number;
  /** The full confirmed BriefInterpretation (connector_plan, proposed_subcategories, …). */
  plan?: Record<string, unknown> | null;
}

export interface EngagementCreateResult {
  engagement_id: string;
  name: string;
  families: number;
  subcategories: number;
  geographies: number;
  sources: number;
  planned_cells: number;
  capped: boolean;
  web_search_enabled: boolean;
}

export interface EngagementPopulateResult {
  engagement_id: string;
  launched: boolean;
  mode: string;
  planned_cells: number;
  detail: string;
}

/** One estimation method in the brief's execution blueprint. */
export interface BriefMethodPlanItem {
  method_code: string;
  tier: string;
  description: string;
  feeds_from: string[];
  methodology: string;
}

/** One phased step of the brief's execution plan. */
export interface BriefExecutionStep {
  step: number;
  phase: string;
  title: string;
  detail: string;
  timeline: string;
}

/** Whether the brief fits the engagement taxonomy or needs a new one. */
export interface BriefTaxonomyStatus {
  in_catalog: boolean;
  proposed_families: string[];
  note: string;
}

/** Structured plan returned by POST /brief/interpret. */
export interface BriefInterpretation {
  families: string[];
  geographies: string[];
  /** Year range: {from: number, to: number} */
  years: { from: number; to: number };
  constraints: string[];
  recommended_sources: BriefRecommendedSource[];
  interpretation_notes: string;
  taxonomy_status?: BriefTaxonomyStatus | null;
  proposed_subcategories?: BriefProposedSubcategory[];
  connector_plan?: BriefConnectorPlanItem[];
  method_plan?: BriefMethodPlanItem[];
  execution_plan?: BriefExecutionStep[];
}
