"use client";

/**
 * Client component: sortable per-source freshness table.
 *
 * Column render functions must live in a client component because they contain
 * JSX — RSC props cannot carry non-serialisable values. Only the `rows` array
 * (plain JSON) is passed down from the server component.
 *
 * Freshness logic mirrors the pipeline's weekly cadence: a source is considered
 * "fresh" when its last_probe_status is OK and last_probe_at is within 8 days
 * (~1 week + 1-day buffer for cron drift). States are classified as recoverable
 * warnings (amber) or hard failures (red) matching the 7-state taxonomy (Q7).
 */

import { DataTable, type Column } from "@/components/DataTable";
import { ConnectorHealthBadge } from "@/components/ConnectorHealthBadge";
import { formatTimestamp } from "@/lib/format";
import type { BackendSourceFreshness, ProbeStatus } from "@/lib/types";

// ---------------------------------------------------------------------------
// Freshness classification
// ---------------------------------------------------------------------------

/** Transient / recoverable states — amber indicator. */
const WARN_STATES = new Set<ProbeStatus>(["RATE_LIMITED", "QUOTA_EXHAUSTED", "EMPTY"]);
/** Hard-failure states — red indicator. */
const ERROR_STATES = new Set<ProbeStatus>(["AUTH_FAILED", "UNREACHABLE", "SCHEMA_MISMATCH"]);

type FreshnessLevel = "fresh" | "stale" | "warning" | "error" | "unknown";

/**
 * Classify a source into a 5-level freshness state.
 * Uses the server-computed `is_stale` flag from `BackendSourceFreshness`
 * (backend checks against _STALE_HOURS = 24*7+1 hours).
 */
function freshnessLevel(sf: BackendSourceFreshness): FreshnessLevel {
  if (!sf.enabled) return "unknown";
  const status = sf.last_probe_status;
  if (!status || !sf.last_probe_at) return "unknown";
  if (ERROR_STATES.has(status)) return "error";
  if (WARN_STATES.has(status)) return "warning";
  // OK — trust the server's is_stale flag
  return sf.is_stale ? "stale" : "fresh";
}

const FRESHNESS_DOT: Record<FreshnessLevel, string> = {
  fresh:   "h-2.5 w-2.5 rounded-full bg-health-ok ring-1 ring-health-ok/30",
  stale:   "h-2.5 w-2.5 rounded-full bg-health-rate ring-1 ring-health-rate/30",
  warning: "h-2.5 w-2.5 rounded-full bg-health-quota ring-1 ring-health-quota/30",
  error:   "h-2.5 w-2.5 rounded-full bg-health-auth ring-1 ring-health-auth/30",
  unknown: "h-2.5 w-2.5 rounded-full bg-ink-subtle ring-1 ring-ink-subtle/20",
};

const FRESHNESS_TITLE: Record<FreshnessLevel, string> = {
  fresh:   "Fresh — probed OK within refresh window",
  stale:   "Stale — probed OK but last probe is old",
  warning: "Warning — transient / recoverable state",
  error:   "Error — hard failure; action required",
  unknown: "Unknown — never probed or disabled",
};

// ---------------------------------------------------------------------------
// Sort weight so errors sort to the top by default
// ---------------------------------------------------------------------------

const PROBE_SORT_ORDER: Record<ProbeStatus | "never" | "disabled", number> = {
  AUTH_FAILED:     0,
  UNREACHABLE:     1,
  SCHEMA_MISMATCH: 2,
  QUOTA_EXHAUSTED: 3,
  RATE_LIMITED:    4,
  EMPTY:           5,
  OK:              6,
  never:           7,
  disabled:        8,
};

function healthSortValue(sf: BackendSourceFreshness): number {
  if (!sf.enabled) return PROBE_SORT_ORDER.disabled;
  if (!sf.last_probe_status) return PROBE_SORT_ORDER.never;
  return PROBE_SORT_ORDER[sf.last_probe_status] ?? 9;
}

// ---------------------------------------------------------------------------
// DataTable column definitions
// ---------------------------------------------------------------------------

const COLUMNS: Column<BackendSourceFreshness>[] = [
  {
    key: "freshness",
    header: "Fresh",
    width: "4.5rem",
    align: "center",
    render: (row) => {
      const level = freshnessLevel(row);
      return (
        <span
          className="flex items-center justify-center"
          title={FRESHNESS_TITLE[level]}
          aria-label={FRESHNESS_TITLE[level]}
        >
          <span className={FRESHNESS_DOT[level]} />
        </span>
      );
    },
    sortValue: (row) => {
      const l = freshnessLevel(row);
      const order: Record<FreshnessLevel, number> = {
        error: 0, stale: 1, warning: 2, unknown: 3, fresh: 4,
      };
      return order[l];
    },
  },
  {
    key: "publisher",
    header: "Publisher",
    sortValue: (row) => row.publisher,
    render: (row) => (
      <span className="font-medium text-ink">{row.publisher}</span>
    ),
  },
  {
    key: "source_id",
    header: "Source ID",
    width: "14rem",
    render: (row) => (
      <span className="font-mono text-2xs text-ink-muted select-all">
        {row.source_id}
      </span>
    ),
    sortValue: (row) => row.source_id,
  },
  {
    key: "health",
    header: "Connector health",
    width: "18rem",
    render: (row) => {
      // Build a rich tooltip: probe detail + budget/quota context.
      const parts: string[] = [];
      if (row.last_probe_detail) parts.push(row.last_probe_detail);
      if (row.monthly_budget != null)
        parts.push(`Budget: $${row.monthly_budget.toLocaleString()}/mo`);
      if (row.quota_ceiling != null)
        parts.push(`Quota ceiling: ${row.quota_ceiling.toLocaleString()} calls`);
      const detail = parts.length > 0 ? parts.join(" · ") : undefined;
      return (
        <ConnectorHealthBadge
          status={row.last_probe_status}
          budgetWarning={row.budget_warning}
          detail={detail}
          size="sm"
        />
      );
    },
    sortValue: healthSortValue,
  },
  {
    key: "last_probe_at",
    header: "Last probed",
    sortValue: (row) => row.last_probe_at ?? "",
    render: (row) => (
      <span
        className="text-xs text-ink-muted tnum"
        title={row.last_probe_at ?? "Never probed"}
      >
        {row.last_probe_at ? formatTimestamp(row.last_probe_at) : "—"}
      </span>
    ),
  },
  {
    key: "enabled",
    header: "Enabled",
    width: "6rem",
    align: "center",
    sortValue: (row) => (row.enabled ? 0 : 1),
    render: (row) => (
      <span
        className={`badge text-2xs ${
          row.enabled
            ? "bg-health-ok-bg text-health-ok"
            : "bg-health-empty-bg text-ink-subtle"
        }`}
      >
        {row.enabled ? "Yes" : "No"}
      </span>
    ),
  },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export interface StatusSourceTableProps {
  /** Serialisable BackendSourceFreshness array passed from the RSC. */
  rows: BackendSourceFreshness[];
}

/**
 * Sortable per-source freshness table for the status dashboard.
 * Default sort order puts hard failures first so issues are immediately visible.
 */
export function StatusSourceTable({ rows }: StatusSourceTableProps) {
  return (
    <DataTable<BackendSourceFreshness>
      columns={COLUMNS}
      rows={rows}
      rowKey={(row) => row.source_id}
      initialSort={{ key: "health", dir: "asc" }}
      empty={
        <span className="text-ink-muted">
          No sources configured yet — add connectors on the{" "}
          <a href="/connectors" className="underline hover:text-ink">
            Connectors
          </a>{" "}
          screen.
        </span>
      }
      dense
    />
  );
}

export default StatusSourceTable;
