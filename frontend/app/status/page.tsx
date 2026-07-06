/**
 * Status page — pipeline freshness per source, 7-state connector health, and
 * cell coverage %.
 *
 * Acceptance criteria from v1-definition §6:
 *   - "status page green within refresh window"
 *   - Every enabled source shows its health state (7-state taxonomy, Q7)
 *   - Budget pre-warning at ~80% of monthly_budget / quota_ceiling (Q7)
 *   - Cell coverage breakdown by confidence level
 *   - Last successful run (latest_raw_at) per source
 *
 * Architecture:
 *   - This is a React Server Component; `dynamic = "force-dynamic"` ensures
 *     every request hits the backend with cache: 'no-store' (already set in
 *     api.getStatus). No stale edge-cache is ever served.
 *   - Interactive sub-components (refresh button, sortable table) are isolated
 *     into client component files so RSC boundaries stay clean.
 *   - Access is intentionally "public-ish": auth is enforced at the layout /
 *     middleware level; this page has no additional role gating.
 */

import type { Metadata } from "next";
import type { ReactNode } from "react";

import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { relativeTime } from "@/lib/format";
import type {
  BackendCellCoverage,
  BackendSourceFreshness,
  BackendStatusSnapshot,
  ProbeStatus,
} from "@/lib/types";

import { StatusRefreshButton } from "./StatusRefreshButton";
import { RefreshDataButton } from "./RefreshDataButton";
import { StatusSourceTable } from "./StatusSourceTable";

export const metadata: Metadata = { title: "Status" };

/**
 * Force dynamic rendering: Next.js must not statically generate or ISR-cache
 * this page. Every request must re-fetch from the backend (cache: 'no-store').
 */
export const dynamic = "force-dynamic";

// ---------------------------------------------------------------------------
// Health aggregate helpers
// ---------------------------------------------------------------------------

const HEALTH_CHIP_STYLES: Partial<
  Record<
    ProbeStatus | "disabled" | "never",
    { bg: string; text: string; dot: string; label: string }
  >
> = {
  OK: {
    bg: "bg-health-ok-bg",
    text: "text-health-ok",
    dot: "bg-health-ok",
    label: "OK",
  },
  AUTH_FAILED: {
    bg: "bg-health-auth-bg",
    text: "text-health-auth",
    dot: "bg-health-auth",
    label: "Auth failed",
  },
  QUOTA_EXHAUSTED: {
    bg: "bg-health-quota-bg",
    text: "text-health-quota",
    dot: "bg-health-quota",
    label: "Quota exhausted",
  },
  RATE_LIMITED: {
    bg: "bg-health-rate-bg",
    text: "text-health-rate",
    dot: "bg-health-rate",
    label: "Rate limited",
  },
  UNREACHABLE: {
    bg: "bg-health-unreachable-bg",
    text: "text-health-unreachable",
    dot: "bg-health-unreachable",
    label: "Unreachable",
  },
  SCHEMA_MISMATCH: {
    bg: "bg-health-schema-bg",
    text: "text-health-schema",
    dot: "bg-health-schema",
    label: "Schema mismatch",
  },
  EMPTY: {
    bg: "bg-health-empty-bg",
    text: "text-health-empty",
    dot: "bg-health-empty",
    label: "Empty",
  },
  disabled: {
    bg: "bg-health-empty-bg",
    text: "text-ink-subtle",
    dot: "bg-ink-subtle",
    label: "Disabled",
  },
  never: {
    bg: "bg-health-empty-bg",
    text: "text-ink-subtle",
    dot: "bg-ink-subtle",
    label: "Never probed",
  },
};

/** Ordered display sequence: errors first, OK last. */
const HEALTH_DISPLAY_ORDER: (ProbeStatus | "disabled" | "never")[] = [
  "AUTH_FAILED",
  "UNREACHABLE",
  "SCHEMA_MISMATCH",
  "QUOTA_EXHAUSTED",
  "RATE_LIMITED",
  "EMPTY",
  "OK",
  "never",
  "disabled",
];

function buildHealthCounts(
  sources: BackendSourceFreshness[],
): Map<ProbeStatus | "disabled" | "never", number> {
  const counts = new Map<ProbeStatus | "disabled" | "never", number>();
  for (const s of sources) {
    if (!s.enabled) {
      counts.set("disabled", (counts.get("disabled") ?? 0) + 1);
    } else if (!s.last_probe_status) {
      counts.set("never", (counts.get("never") ?? 0) + 1);
    } else {
      counts.set(
        s.last_probe_status,
        (counts.get(s.last_probe_status) ?? 0) + 1,
      );
    }
  }
  return counts;
}

// ---------------------------------------------------------------------------
// Freshness summary
// ---------------------------------------------------------------------------

function countFreshSources(sources: BackendSourceFreshness[]): {
  fresh: number;
  enabled: number;
} {
  const enabled = sources.filter((s) => s.enabled);
  // The backend computes is_stale; use it when available, fall back to client-side check.
  const fresh = enabled.filter(
    (s) => s.last_probe_status === "OK" && !s.is_stale,
  );
  return { fresh: fresh.length, enabled: enabled.length };
}

// ---------------------------------------------------------------------------
// Sub-components (server-renderable; no client state required)
// ---------------------------------------------------------------------------

/** Single stat card used in the coverage summary row. */
function StatCard({
  label,
  value,
  sub,
  accent = "text-ink",
}: {
  label: string;
  value: string;
  sub?: string;
  accent?: string;
}) {
  return (
    <div className="card flex flex-col gap-1 p-4">
      <span className="eyebrow text-ink-subtle">{label}</span>
      <span className={`text-2xl font-semibold tnum leading-none ${accent}`}>
        {value}
      </span>
      {sub && (
        <span className="mt-0.5 text-xs text-ink-muted tnum">{sub}</span>
      )}
    </div>
  );
}

/**
 * Proportional stacked coverage bar built from the backend's
 * `cells_by_confidence` dict (keys: "high" | "medium" | "low" | "none").
 */
function CoverageBar({ coverage }: { coverage: BackendCellCoverage }) {
  const { total_cells, cells_by_confidence } = coverage;
  const high = cells_by_confidence["high"] ?? 0;
  const medium = cells_by_confidence["medium"] ?? 0;
  const low = cells_by_confidence["low"] ?? 0;
  const none = cells_by_confidence["none"] ?? 0;

  if (total_cells === 0) {
    return (
      <div className="h-3 w-full overflow-hidden rounded-full bg-surface-subtle">
        <span className="sr-only">No cells yet</span>
      </div>
    );
  }
  const pct = (n: number) => `${((n / total_cells) * 100).toFixed(2)}%`;
  return (
    <div
      className="flex h-3 w-full overflow-hidden rounded-full"
      role="img"
      aria-label={`Coverage: ${high} high, ${medium} medium, ${low} low, ${none} unsized`}
    >
      {high > 0 && (
        <div
          style={{ width: pct(high) }}
          className="bg-confidence-high"
          title={`High confidence: ${high}`}
        />
      )}
      {medium > 0 && (
        <div
          style={{ width: pct(medium) }}
          className="bg-confidence-medium"
          title={`Medium confidence: ${medium}`}
        />
      )}
      {low > 0 && (
        <div
          style={{ width: pct(low) }}
          className="bg-confidence-low"
          title={`Low confidence: ${low}`}
        />
      )}
      {none > 0 && (
        <div
          style={{ width: pct(none) }}
          className="bg-line"
          title={`Unsized: ${none}`}
        />
      )}
    </div>
  );
}

/** Legend dot + label for the coverage bar key. */
function LegendItem({
  dotClass,
  label,
}: {
  dotClass: string;
  label: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink-muted">
      <span className={`h-2 w-2 shrink-0 rounded-full ${dotClass}`} aria-hidden />
      {label}
    </span>
  );
}

/**
 * Aggregate chip for a single probe state + count of sources in that state.
 * E.g. "OK 12" or "Auth failed 2".
 */
function HealthCountChip({
  stateKey,
  count,
}: {
  stateKey: ProbeStatus | "disabled" | "never";
  count: number;
}) {
  const style = HEALTH_CHIP_STYLES[stateKey];
  if (!style) return null;
  return (
    <span className={`badge ${style.bg} ${style.text}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} aria-hidden />
      <span>{style.label}</span>
      <span className="font-semibold tnum">{count}</span>
    </span>
  );
}

/** Full-width error banner shown when the backend is unreachable. */
function ErrorBanner({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="card flex items-start gap-3 border-l-4 border-health-auth p-4"
    >
      <svg
        viewBox="0 0 24 24"
        className="mt-0.5 h-5 w-5 shrink-0 text-health-auth"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="8" x2="12" y2="12" />
        <line x1="12" y1="16" x2="12.01" y2="16" />
      </svg>
      <div>
        <p className="text-sm font-semibold text-health-auth">
          Status snapshot unavailable
        </p>
        <p className="mt-0.5 text-sm text-ink-muted">{message}</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default async function StatusPage(): Promise<ReactNode> {
  // Two parallel no-store fetches: aggregate snapshot + per-source detail.
  let snapshot: BackendStatusSnapshot | null = null;
  let sources: BackendSourceFreshness[] = [];
  let fetchError: string | null = null;

  try {
    // Run both calls in parallel; either can independently fail.
    const [snapshotResult, sourcesResult] = await Promise.allSettled([
      api.getStatusSnapshot(),
      api.listSourceFreshness(),
    ]);

    if (snapshotResult.status === "fulfilled") {
      snapshot = snapshotResult.value;
    } else {
      const err = snapshotResult.reason;
      fetchError =
        err instanceof ApiError
          ? `HTTP ${err.status}: ${err.message}`
          : "Unable to reach the backend API. Verify the service is running.";
    }

    if (sourcesResult.status === "fulfilled") {
      sources = sourcesResult.value;
    } else if (!fetchError) {
      // Only report secondary error if the primary succeeded (avoid double banner).
      const err = sourcesResult.reason;
      fetchError =
        err instanceof ApiError
          ? `Source-freshness fetch failed — HTTP ${err.status}: ${err.message}`
          : "Source freshness list unavailable.";
    }
  } catch (err) {
    fetchError =
      err instanceof ApiError
        ? `HTTP ${err.status ? `${err.status}: ` : ""}${err.message}`
        : "Unexpected error fetching status data.";
  }

  const coverage: BackendStatusSnapshot["cell_coverage"] | null =
    snapshot?.cell_coverage ?? null;
  const healthCounts = buildHealthCounts(sources);
  const { fresh, enabled } = countFreshSources(sources);

  const descriptionText = snapshot
    ? `Snapshot generated ${relativeTime(new Date(snapshot.generated_at))} · ${fresh} of ${enabled} enabled source${enabled !== 1 ? "s" : ""} fresh`
    : "Pipeline freshness per source, 7-state connector health, and cell coverage.";

  return (
    <div className="space-y-8">
      {/* ------------------------------------------------------------------ */}
      {/* Header                                                              */}
      {/* ------------------------------------------------------------------ */}
      <PageHeader
        eyebrow="Observability"
        title="Status"
        description={descriptionText}
        actions={
          <div className="flex items-center gap-2">
            {snapshot && (
              <span
                className={`badge ${snapshot.pipeline_healthy ? "bg-health-ok-bg text-health-ok" : "bg-health-auth-bg text-health-auth"}`}
              >
                <span
                  className={`h-1.5 w-1.5 rounded-full ${snapshot.pipeline_healthy ? "bg-health-ok" : "bg-health-auth"}`}
                  aria-hidden
                />
                {snapshot.pipeline_healthy ? "Healthy" : "Degraded"}
              </span>
            )}
            <StatusRefreshButton />
            <RefreshDataButton />
          </div>
        }
      />

      {/* ------------------------------------------------------------------ */}
      {/* Error banner                                                        */}
      {/* ------------------------------------------------------------------ */}
      {fetchError && <ErrorBanner message={fetchError} />}

      {/* ------------------------------------------------------------------ */}
      {/* Coverage summary                                                    */}
      {/* ------------------------------------------------------------------ */}
      {coverage && (
        <section aria-labelledby="coverage-heading" className="space-y-4">
          <h2 id="coverage-heading" className="eyebrow">
            Cell coverage
          </h2>

          {/* Stat cards row */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            <StatCard
              label="Total cells"
              value={coverage.total_cells.toLocaleString()}
              accent="text-ink"
            />
            <StatCard
              label="Cells sized"
              value={coverage.cells_with_estimates.toLocaleString()}
              sub={`${coverage.coverage_pct.toFixed(1)}% coverage`}
              accent="text-brand-600"
            />
            <StatCard
              label="High confidence"
              value={(coverage.cells_by_confidence["high"] ?? 0).toLocaleString()}
              sub={
                coverage.total_cells > 0
                  ? `${(((coverage.cells_by_confidence["high"] ?? 0) / coverage.total_cells) * 100).toFixed(1)}%`
                  : undefined
              }
              accent="text-confidence-high"
            />
            <StatCard
              label="Medium confidence"
              value={(coverage.cells_by_confidence["medium"] ?? 0).toLocaleString()}
              sub={
                coverage.total_cells > 0
                  ? `${(((coverage.cells_by_confidence["medium"] ?? 0) / coverage.total_cells) * 100).toFixed(1)}%`
                  : undefined
              }
              accent="text-confidence-medium"
            />
            <StatCard
              label="Unsized / Low"
              value={(
                (coverage.cells_by_confidence["none"] ?? 0) +
                (coverage.cells_by_confidence["low"] ?? 0)
              ).toLocaleString()}
              sub={
                coverage.total_cells > 0
                  ? `${((((coverage.cells_by_confidence["none"] ?? 0) + (coverage.cells_by_confidence["low"] ?? 0)) / coverage.total_cells) * 100).toFixed(1)}%`
                  : undefined
              }
              accent="text-ink-subtle"
            />
          </div>

          {/* Proportional stacked bar */}
          <div className="card space-y-3 p-4">
            <CoverageBar coverage={coverage} />
            <div className="flex flex-wrap gap-4">
              <LegendItem dotClass="bg-confidence-high" label="High confidence" />
              <LegendItem dotClass="bg-confidence-medium" label="Medium confidence" />
              <LegendItem dotClass="bg-confidence-low" label="Low confidence" />
              <LegendItem dotClass="bg-line" label="Unsized" />
            </div>
          </div>
        </section>
      )}

      {/* Show placeholder when snapshot loaded but no cells exist yet */}
      {snapshot && !coverage && (
        <div className="card p-6 text-center text-sm text-ink-muted">
          No cell coverage data available yet.
        </div>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Connector health overview (aggregate counts per state)              */}
      {/* ------------------------------------------------------------------ */}
      <section aria-labelledby="health-heading" className="space-y-4">
        <h2 id="health-heading" className="eyebrow">
          Connector health overview
        </h2>

        {sources.length > 0 ? (
          <div className="card flex flex-wrap items-center gap-2 p-4">
            {HEALTH_DISPLAY_ORDER.map((key) => {
              const count = healthCounts.get(key) ?? 0;
              if (count === 0) return null;
              return <HealthCountChip key={key} stateKey={key} count={count} />;
            })}
          </div>
        ) : (
          <div className="card p-4 text-sm text-ink-muted">
            No connectors configured.{" "}
            <a href="/connectors" className="underline hover:text-ink">
              Add one on the Connectors screen.
            </a>
          </div>
        )}
      </section>

      {/* ------------------------------------------------------------------ */}
      {/* Per-source freshness table                                          */}
      {/* ------------------------------------------------------------------ */}
      <section aria-labelledby="sources-heading" className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="sources-heading" className="eyebrow">
            Source freshness
          </h2>
          {sources.length > 0 && (
            <span className="text-xs text-ink-muted">
              {sources.length} source{sources.length !== 1 ? "s" : ""}
              {enabled < sources.length
                ? ` · ${sources.length - enabled} disabled`
                : ""}
            </span>
          )}
        </div>

        {/*
          StatusSourceTable is a client component so DataTable can sort.
          Only the serialisable `rows` array crosses the RSC boundary;
          column render functions live inside the client component.
        */}
        <StatusSourceTable rows={sources} />
      </section>

      {/* ------------------------------------------------------------------ */}
      {/* Data notes footer                                                   */}
      {/* ------------------------------------------------------------------ */}
      <footer className="text-xs text-ink-subtle">
        <p>
          Freshness indicator is green when a source has status&nbsp;OK and
          last_probe_at is within the refresh window (server-computed
          using&nbsp;an 8-day threshold). Budget warning&nbsp;(🟠) surfaces
          QUOTA_EXHAUSTED on sources with a cost ceiling&nbsp;(Q7). Confidence
          is computed solely by the{" "}
          <code className="font-mono text-2xs">cell_triangulation_summary</code>{" "}
          materialised view reading the active validation profile — never set
          manually.
        </p>
      </footer>
    </div>
  );
}
