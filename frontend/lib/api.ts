/**
 * Typed fetch client for the GRX10 FastAPI backend.
 *
 * Base URL comes from NEXT_PUBLIC_API_BASE_URL (wired by render.yaml). Designed
 * for React Server Components: every call is a plain `fetch` so Next.js can
 * cache/revalidate it. Client components should use SWR with these functions as
 * fetchers (see `useApi` in lib/swr.ts when a screen needs live data).
 *
 * NOTE for screen agents: the endpoint paths below are the agreed contract with
 * the backend router agents. If a route differs, change it HERE only — never
 * hand-roll fetches in a screen.
 */

import type {
  AddSourcePayload,
  Assumption,
  BackendSourceFreshness,
  BackendStatusSnapshot,
  BriefInterpretation,
  Cell,
  CellQuery,
  CellTriangulationView,
  CellView,
  Company,
  Engagement,
  EngagementCreate,
  EngagementCreateResult,
  EngagementPopulateResult,
  Geography,
  MethodRegistryEntry,
  Paginated,
  PlayerShare,
  RecommendedSources,
  SetCredentialResponse,
  Source,
  SourceDetail,
  StatusSnapshot,
  SuggestMappingRequest,
  SuggestMappingResponse,
  SupplierRelationship,
  TaxonomySubcategory,
  ValidationProfile,
} from "./types";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ??
  "http://localhost:8000";

/** Error thrown for any non-2xx API response, carrying status + parsed body. */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

type Primitive = string | number | boolean | null | undefined;

export interface RequestOptions extends Omit<RequestInit, "body" | "next"> {
  /** Query-string params; undefined/null values are dropped. */
  query?: Record<string, Primitive>;
  /** JSON body (object) — serialised + content-type set automatically. */
  json?: unknown;
  /**
   * Next.js fetch cache hint. Defaults to `{ revalidate: 60 }` for GETs so list
   * views stay sub-second (acceptance criterion) without serving stale data for
   * long. Pass `{ cache: "no-store" }` for always-fresh status reads.
   */
  next?: { revalidate?: number; tags?: string[] };
}

function buildUrl(path: string, query?: Record<string, Primitive>): string {
  const url = new URL(
    path.startsWith("/") ? path : `/${path}`,
    `${API_BASE_URL}/`,
  );
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null) continue;
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/** Low-level typed request. All higher-level helpers funnel through this. */
/**
 * Resolve the active-engagement header so every request is scoped to the
 * engagement the user selected. Isomorphic: on the server (RSC/page fetches) we
 * read the incoming request's cookie via `next/headers`; on the client we read
 * `document.cookie`. Absent → backend defaults to the Medtech demo. The dynamic
 * import is guarded by the window check so `next/headers` never enters the
 * client bundle.
 */
async function engagementHeader(): Promise<Record<string, string>> {
  try {
    if (typeof window === "undefined") {
      const { cookies } = await import("next/headers");
      const id = cookies().get("engagement_id")?.value;
      return id ? { "X-Engagement-Id": id } : {};
    }
    const m = document.cookie.match(/(?:^|;\s*)engagement_id=([^;]+)/);
    return m ? { "X-Engagement-Id": decodeURIComponent(m[1]) } : {};
  } catch {
    return {};
  }
}

export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { query, json, headers, next, ...rest } = options;
  const engHeader = await engagementHeader();
  const init: RequestInit & { next?: RequestOptions["next"] } = {
    ...rest,
    headers: {
      Accept: "application/json",
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...engHeader,
      ...headers,
    },
  };
  if (json !== undefined) init.body = JSON.stringify(json);
  // Default caching: always-fresh (no-store) so the live demo never serves a
  // stale model after a pipeline run / data reload. Respect explicit overrides.
  const method = (rest.method ?? "GET").toUpperCase();
  if (method === "GET" && rest.cache === undefined && next === undefined) {
    init.cache = "no-store";
  } else if (next !== undefined) {
    init.next = next;
  }

  let res: Response;
  try {
    res = await fetch(buildUrl(path, query), init);
  } catch (cause) {
    throw new ApiError(0, `Network error reaching ${path}`, cause);
  }

  const isJson = res.headers
    .get("content-type")
    ?.includes("application/json");
  const payload = isJson ? await res.json().catch(() => null) : await res.text();

  if (!res.ok) {
    const detail =
      (payload && typeof payload === "object" && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : res.statusText) || `Request to ${path} failed`;
    throw new ApiError(res.status, detail, payload);
  }
  return payload as T;
}

// ===========================================================================
// Endpoint helpers — the typed surface screen agents consume.
// ===========================================================================

export const api = {
  // --- Cells (Cell Explorer + Cell Detail) ---------------------------------
  listCells: (q: CellQuery = {}) =>
    apiRequest<Paginated<CellView>>("/cells", { query: q as Record<string, Primitive> }),

  getCell: (cellId: number) => apiRequest<CellView>(`/cells/${cellId}`),

  /** The audit chain: one row per (method, source) with drill metadata. */
  getCellTriangulation: (cellId: number) =>
    apiRequest<CellTriangulationView[]>(`/cells/${cellId}/triangulation`),

  // --- Players -------------------------------------------------------------
  /**
   * Backend returns a paginated PlayerShareList with nested company objects and
   * Decimal fields serialised as strings. We unwrap items, flatten company.name
   * to company_name, and coerce NUMERIC strings with Number().
   */
  listPlayerShares: (cellId: number) =>
    apiRequest<{
      items: Array<{
        share_id: number;
        cell_id: number;
        company_id: number;
        player_role: string;
        rank: number;
        share_pct: string | null;
        share_low_pct: string | null;
        share_high_pct: string | null;
        revenue_usd_m: string | null;
        source_id: string;
        confidence: string | null;
        company: { name: string } | null;
      }>;
    }>(`/cells/${cellId}/players`).then((r) =>
      r.items.map(
        (s) =>
          ({
            ...s,
            company_name: s.company?.name ?? "",
            share_pct: s.share_pct != null ? Number(s.share_pct) : null,
            share_low_pct:
              s.share_low_pct != null ? Number(s.share_low_pct) : null,
            share_high_pct:
              s.share_high_pct != null ? Number(s.share_high_pct) : null,
            revenue_usd_m:
              s.revenue_usd_m != null ? Number(s.revenue_usd_m) : null,
          }) as PlayerShare,
      ),
    ),

  /**
   * Backend endpoint is GET /cells/{cellId}/supplier-relationships (paginated).
   * Each item carries nested buyer/supplier company objects; we flatten to
   * buyer_name and supplier_name for the frontend type.
   */
  listSupplierRelationships: (cellId?: number) =>
    !cellId
      ? Promise.resolve([] as SupplierRelationship[])
      : apiRequest<{
          items: Array<{
            relationship_id: number;
            buyer_id: number;
            supplier_id: number;
            cell_id: number | null;
            relationship_type: string;
            evidence_type: string;
            evidence_strength: string;
            source_id: string;
            notes: string | null;
            buyer: { name: string } | null;
            supplier: { name: string } | null;
          }>;
        }>(`/cells/${cellId}/supplier-relationships`).then((r) =>
          r.items.map(
            (rel) =>
              ({
                ...rel,
                buyer_name: rel.buyer?.name ?? "",
                supplier_name: rel.supplier?.name ?? "",
              }) as SupplierRelationship,
          ),
        ),

  // --- Connectors / sources -----------------------------------------------
  listSources: () => apiRequest<Source[]>("/connectors"),

  getSource: (sourceId: string) =>
    apiRequest<Source>(`/connectors/${encodeURIComponent(sourceId)}`),

  /** Trigger a cheap probe() and return the refreshed source row (admin-gated). */
  probeSource: (sourceId: string) =>
    apiRequest<Source>(`/connectors/${encodeURIComponent(sourceId)}/probe`, {
      method: "POST",
    }),

  /**
   * Register a new custom source row. For generic-REST connectors the backend
   * stores `field_mappings` alongside the source. Returns the created Source.
   */
  addSource: (payload: AddSourcePayload) =>
    apiRequest<Source>("/connectors", { method: "POST", json: payload }),

  /**
   * Write (or rotate) the API credential for a source. The secret is
   * envelope-encrypted on the backend; it is NEVER returned to the browser.
   * Only callable by owner/admin (403 otherwise).
   */
  setCredential: (sourceId: string, secret: string) =>
    apiRequest<SetCredentialResponse>(
      `/connectors/${encodeURIComponent(sourceId)}/credential`,
      { method: "POST", json: { secret } },
    ),

  /**
   * AI-assisted field-mapping suggestion. Send the URL pattern + target table
   * (+ optional sample JSON) and get back Claude-suggested column mappings.
   */
  suggestMapping: (req: SuggestMappingRequest) =>
    apiRequest<SuggestMappingResponse>("/connectors/suggest-mapping", {
      method: "POST",
      json: req,
    }),

  /** Enable a previously-disabled source (admin-gated). */
  enableSource: (sourceId: string) =>
    apiRequest<Source>(`/connectors/${encodeURIComponent(sourceId)}/enable`, {
      method: "POST",
    }),

  /** Disable a source without deleting it (admin-gated). */
  disableSource: (sourceId: string) =>
    apiRequest<Source>(`/connectors/${encodeURIComponent(sourceId)}/disable`, {
      method: "POST",
    }),

  // --- Sources view (W2) — /sources registry with rationale + method usage --
  /**
   * Every source row enriched with a plain-English "why it matters" rationale
   * and a "used_for" list of method_codes that draw from this source's raw_table.
   * Grouped by class (A primary → B cross-check → C triangulation) on the server.
   */
  listSourceDetails: () =>
    apiRequest<SourceDetail[]>("/sources"),

  /**
   * Method-code → [source_ids] map for the "Recommended sources by method"
   * section.  Pass an optional family name to scope (currently accepted but
   * not yet filtered server-side — returns all methods).
   */
  getRecommendedSources: (family?: string) =>
    apiRequest<RecommendedSources>("/sources/recommended", {
      query: family ? { family } : undefined,
    }),

  // --- Assumptions ledger --------------------------------------------------
  /** Backend returns a paginated AssumptionList; unwrap items here. */
  listAssumptions: () =>
    apiRequest<{ items: Assumption[] }>("/assumptions").then((r) => r.items),

  /** Reverse drill: cells influenced by an assumption (via cell_assumption_link). */
  getAssumptionCells: (assumptionId: number) =>
    apiRequest<Cell[]>(`/assumptions/${assumptionId}/cells`),

  /**
   * Create a new assumption. When `supersedes_id` is provided the backend must
   * atomically INSERT the new row and UPDATE assumptions SET superseded_by =
   * <new_id> WHERE assumption_id = supersedes_id, preserving the full version
   * chain without deleting prior data.
   *
   * Integrator note: wire this in POST /assumptions in the FastAPI router.
   */
  createAssumption: (body: {
    assumption_text: string;
    numeric_value?: number | null;
    unit?: string | null;
    confidence?: string | null;
    derivation_method?: string | null;
    source_id?: string | null;
    effective_from_year: number;
    effective_to_year?: number | null;
    scope_company_id?: number | null;
    scope_subcategory_id?: number | null;
    scope_geography_id?: number | null;
    /** When set, old assumption's superseded_by is updated to point to new row. */
    supersedes_id?: number | null;
  }) =>
    apiRequest<Assumption>("/assumptions", { method: "POST", json: body }),

  // --- Reference / spine ---------------------------------------------------
  listGeographies: () => apiRequest<Geography[]>("/reference/geographies"),
  listSubcategories: () =>
    apiRequest<TaxonomySubcategory[]>("/reference/subcategories"),
  listCompanies: () => apiRequest<Company[]>("/reference/companies"),
  listMethods: () => apiRequest<MethodRegistryEntry[]>("/reference/methods"),

  // --- Settings / validation profiles --------------------------------------
  /** Backend: GET /settings/profiles */
  listValidationProfiles: () =>
    apiRequest<ValidationProfile[]>("/settings/profiles"),

  /** Backend: PUT /settings/profiles/{id}/activate (profile_id in path, no body) */
  setActiveProfile: (profileId: number) =>
    apiRequest<ValidationProfile>(`/settings/profiles/${profileId}/activate`, {
      method: "PUT",
    }),

  /**
   * Clone a validation profile (owner/admin only).
   * Backend: POST /settings/profiles/clone → { source_profile_id, name, ...overrides }
   */
  createValidationProfile: (body: Omit<ValidationProfile, "profile_id">) =>
    apiRequest<ValidationProfile>("/settings/profiles/clone", {
      method: "POST",
      json: body,
    }),

  /**
   * Per-engagement web-search fallback status (Q8).
   * Enabled by default; owner/admin may disable per engagement.
   * Backend: GET /settings/web-search → { enabled: bool, web_search_source_count: int }
   */
  getWebSearchConfig: () =>
    apiRequest<{ enabled: boolean }>("/settings/web-search", {
      cache: "no-store",
    }),

  /**
   * Toggle web-search fallback for this engagement (owner/admin only).
   * Backend: PUT /settings/web-search { enabled } → { enabled: bool }
   */
  setWebSearchEnabled: (enabled: boolean) =>
    apiRequest<{ enabled: boolean }>("/settings/web-search", {
      method: "PUT",
      json: { enabled },
    }),

  /**
   * Audience preview preference (session-level, stored server-side).
   * Values: "analyst" | "business" | "external" | "all".
   * Backend: GET /settings/audience → { audience: string }
   */
  getAudiencePref: () =>
    apiRequest<{ audience: string }>("/settings/audience", {
      cache: "no-store",
    }),

  /** Set the audience preview preference. Backend: PUT /settings/audience */
  setAudiencePref: (audience: string) =>
    apiRequest<{ audience: string }>("/settings/audience", {
      method: "PUT",
      json: { audience },
    }),

  // --- Status --------------------------------------------------------------
  /**
   * @deprecated Return type `StatusSnapshot` does not match the actual backend
   * shape. Use `getStatusSnapshot()` + `listSourceFreshness()` instead.
   */
  getStatus: () =>
    apiRequest<StatusSnapshot>("/status", { cache: "no-store" }),

  /**
   * Aggregate pipeline-health snapshot: connector-health counts, cell-coverage
   * stats, pipeline_healthy flag, and generated_at timestamp.
   * Mirrors `StatusResponse` from `backend/app/routers/status.py`.
   * Always fetched with cache: 'no-store' (status page is always-live).
   */
  getStatusSnapshot: () =>
    apiRequest<BackendStatusSnapshot>("/status", { cache: "no-store" }),

  /**
   * Per-source freshness list: one row per source, ordered by last_probe_at
   * desc (most-recent first, never-probed last).  Includes staleness flag and
   * Q7 budget-warning flag.
   * Mirrors `list[SourceFreshnessItem]` from `backend/app/routers/status.py`.
   */
  listSourceFreshness: (enabledOnly = false) =>
    apiRequest<BackendSourceFreshness[]>("/status/sources", {
      cache: "no-store",
      query: enabledOnly ? { enabled_only: true } : undefined,
    }),

  // --- Stats / dashboard aggregates ---------------------------------------
  /**
   * Dashboard headline aggregates: total TAM, cell count, confidence breakdown
   * (count + TAM per band), TAM by product family, TAM by geography.
   * Served by GET /stats/overview (backend/app/routers/stats.py).
   * Year defaults to 2026 (the first model year in the Medtech APAC dataset).
   */
  getStatsOverview: (year?: number) =>
    apiRequest<StatsOverview>("/stats/overview", {
      query: year !== undefined ? { year } : undefined,
    }),

  // --- Reports + Excel exports --------------------------------------------
  /**
   * Trigger generation of one of the three standard PDFs.
   * The backend synthesises the report from current DB state and returns a
   * short-lived download URL. POST body params are optional — omitting them
   * runs against the full engagement scope.
   * Acceptance criterion: PDFs must include a numbered, clickable Sources page.
   */
  generateStandardReport: (
    type: StandardReportType,
    params: {
      year?: number;
      subcategory_ids?: number[];
      geography_ids?: number[];
    } = {},
  ) =>
    apiRequest<ReportResult>(`/reports/${type}`, {
      method: "POST",
      json: params,
    }),

  /**
   * Generate a custom PDF from an ordered list of section IDs.
   * The backend appends "sources_reference" if absent (numbered Sources page
   * is a hard acceptance criterion).
   */
  generateCustomReport: (
    sections: string[],
    params: { year?: number } = {},
  ) =>
    apiRequest<ReportResult>("/reports/custom", {
      method: "POST",
      json: { sections, ...params },
    }),

  /**
   * Trigger an Excel export in one of five flavours.
   * Every export includes a _README sheet carrying scope, timestamp, and
   * methodology (acceptance criterion). The download_url is a direct GET link
   * to the generated .xlsx file.
   */
  generateExcelExport: (
    flavor: ExcelFlavor,
    params: { year?: number } = {},
  ) =>
    apiRequest<ReportResult>(`/exports/excel/${flavor}`, {
      method: "POST",
      json: params,
    }),

  // --- NL Brief interpreter (W3) ------------------------------------------
  /**
   * POST /brief/interpret — parse a free-text research brief into a structured
   * plan: families, geographies, year range, constraints, and recommended
   * sources. Uses Claude when ANTHROPIC_API_KEY is set; falls back to a
   * deterministic rule-based interpreter otherwise. Never fails.
   */
  interpretBrief: (text: string) =>
    apiRequest<BriefInterpretation>("/brief/interpret", {
      method: "POST",
      json: { text },
    }),

  // ── Engagements (multi-engagement switcher + create-from-brief) ────────────
  listEngagements: (includeArchived = false) =>
    apiRequest<Engagement[]>("/engagements", {
      query: includeArchived ? { include_archived: true } : undefined,
      cache: "no-store",
    }),

  currentEngagement: () =>
    apiRequest<Engagement>("/engagements/current", { cache: "no-store" }),

  createEngagement: (body: EngagementCreate) =>
    apiRequest<EngagementCreateResult>("/engagements", {
      method: "POST",
      json: body,
    }),

  activateEngagement: (engagementId: string) =>
    apiRequest<Engagement>(
      `/engagements/${encodeURIComponent(engagementId)}/activate`,
      { method: "POST" },
    ),

  archiveEngagement: (engagementId: string) =>
    apiRequest<Engagement>(
      `/engagements/${encodeURIComponent(engagementId)}/archive`,
      { method: "POST" },
    ),

  populateEngagement: (engagementId: string) =>
    apiRequest<EngagementPopulateResult>(
      `/engagements/${encodeURIComponent(engagementId)}/populate`,
      { method: "POST" },
    ),
} as const;

// ── Report / export types (shared between api.ts and the reports screen) ───

/** The three standard PDF report types. */
export type StandardReportType =
  | "executive-audit"
  | "gap-analysis"
  | "player-shares";

/**
 * Five Excel export flavours.
 * "full" bundles all four subject sheets plus the mandatory _README sheet.
 */
export type ExcelFlavor =
  | "cells"
  | "triangulation"
  | "players"
  | "assumptions"
  | "full";

/**
 * Shape returned by all /reports/* and /exports/excel/* POST endpoints.
 * The download_url points to the generated file; expires_at is null when the
 * link does not time out (e.g. Render persistent storage vs. presigned S3).
 */
export interface ReportResult {
  download_url: string;
  generated_at: string;
  report_type: string;
  expires_at: string | null;
}

// ── Stats / dashboard types (api.getStatsOverview) ─────────────────────────

export interface StatsConfidenceSplit {
  count: number;
  tam_usd_m: number;
}

/**
 * Shape returned by GET /stats/overview.
 * All numeric fields are plain floats (never Decimal strings) — the backend
 * coerces Postgres NUMERIC before serialisation.
 */
export interface StatsOverview {
  year: number;
  total_tam_usd_m: number;
  cell_count: number;
  confidence_breakdown: {
    high: StatsConfidenceSplit;
    medium: StatsConfidenceSplit;
    low: StatsConfidenceSplit;
  };
  by_family: Array<{ family: string; tam_usd_m: number; share: number }>;
  by_geography: Array<{ country: string; tam_usd_m: number; share: number }>;
}

export type Api = typeof api;
