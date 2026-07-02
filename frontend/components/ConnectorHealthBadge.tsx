import type { ProbeStatus } from "@/lib/types";

/**
 * 7-state connector-health badge (Q7) plus the cost/budget pre-warning.
 * States and colours match `connectors/base.py::ProbeStatus` and the
 * `sources.last_probe_status` column exactly.
 */

const STATES: Record<ProbeStatus, { label: string; cls: string; dot: string }> = {
  OK: { label: "OK", cls: "bg-health-ok-bg text-health-ok", dot: "bg-health-ok" },
  AUTH_FAILED: {
    label: "Auth failed",
    cls: "bg-health-auth-bg text-health-auth",
    dot: "bg-health-auth",
  },
  QUOTA_EXHAUSTED: {
    label: "Quota exhausted",
    cls: "bg-health-quota-bg text-health-quota",
    dot: "bg-health-quota",
  },
  RATE_LIMITED: {
    label: "Rate limited",
    cls: "bg-health-rate-bg text-health-rate",
    dot: "bg-health-rate",
  },
  UNREACHABLE: {
    label: "Unreachable",
    cls: "bg-health-unreachable-bg text-health-unreachable",
    dot: "bg-health-unreachable",
  },
  SCHEMA_MISMATCH: {
    label: "Schema mismatch",
    cls: "bg-health-schema-bg text-health-schema",
    dot: "bg-health-schema",
  },
  EMPTY: {
    label: "Empty",
    cls: "bg-health-empty-bg text-health-empty",
    dot: "bg-health-empty",
  },
};

export interface ConnectorHealthBadgeProps {
  status: ProbeStatus | null | undefined;
  /** 🟠 budget pre-warning at ~80% of monthly_budget / quota_ceiling (Q7). */
  budgetWarning?: boolean;
  /** probe detail string for the title tooltip. */
  detail?: string | null;
  size?: "sm" | "md";
  className?: string;
}

export function ConnectorHealthBadge({
  status,
  budgetWarning = false,
  detail,
  size = "md",
  className = "",
}: ConnectorHealthBadgeProps) {
  const pad = size === "sm" ? "px-1.5 py-0 text-2xs" : "";

  if (!status) {
    return (
      <span
        className={`badge bg-health-empty-bg text-ink-subtle ${pad} ${className}`}
        title="Never probed"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-ink-subtle" aria-hidden />
        Not probed
      </span>
    );
  }

  const s = STATES[status];
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className={`badge ${s.cls} ${pad} ${className}`}
        title={detail ?? s.label}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${s.dot}`} aria-hidden />
        {s.label}
      </span>
      {budgetWarning && (
        <span
          className={`badge bg-health-budget-bg text-health-budget ${pad}`}
          title="Approaching budget / quota ceiling (~80%)"
        >
          <span aria-hidden>🟠</span> Budget
        </span>
      )}
    </span>
  );
}

export default ConnectorHealthBadge;
