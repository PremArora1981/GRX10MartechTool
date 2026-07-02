"use client";

/**
 * EstimatesPanel — the interactive heart of the two-click audit chain.
 *
 * Architecture:
 *   Click 1 — a row in the estimates table expands an inline SourcePanel
 *              showing publisher, source class, URL, access method, and
 *              the timestamp when the estimate was computed.
 *   Click 2 — "View raw payload" in the SourcePanel opens the verbatim
 *              JSONB record via GET /cells/{id}/triangulation/{id}/raw
 *              (backend endpoint; see integrator notes).
 *
 * Rows are sorted Tier A → B → C, then by estimate descending within each
 * tier. Summary statistics (distinct methods, spread ratio) are computed
 * from the data and shown above the table so analysts can assess convergence
 * at a glance before drilling into individual estimates.
 *
 * This is a client component because the accordion interaction requires
 * local React state. Data is fetched server-side (in page.tsx) and passed
 * as props so the initial render is fast and the page is fully SSR'd.
 */

import { Fragment, useMemo, useState } from "react";
import { API_BASE_URL } from "@/lib/api";
import { formatSpread, formatTimestamp, formatUsdMillions } from "@/lib/format";
import type { CellTriangulationView, MethodTier, SourceClass } from "@/lib/types";
import { DrillLink } from "@/components";

// ---------------------------------------------------------------------------
// Local badge primitives (scoped to this module — not in the shared design
// system because tier / source-class badges are only needed here).
// ---------------------------------------------------------------------------

const TIER_STYLE: Record<MethodTier, { cls: string; title: string }> = {
  A: {
    cls: "bg-confidence-high-bg text-confidence-high",
    title: "Tier A — primary structured source; qualifies HIGH confidence",
  },
  B: {
    cls: "bg-confidence-medium-bg text-confidence-medium",
    title: "Tier B — industry / procedural; qualifies MEDIUM confidence",
  },
  C: {
    cls: "bg-confidence-low-bg text-confidence-low",
    title: "Tier C — triangulation support only; gap-fill / scaling",
  },
};

const CLASS_STYLE: Record<SourceClass, { cls: string; long: string }> = {
  A: {
    cls: "bg-confidence-high-bg text-confidence-high",
    long: "Class A — Primary structured (qualifies HIGH)",
  },
  B: {
    cls: "bg-confidence-medium-bg text-confidence-medium",
    long: "Class B — Industry / procedural (qualifies MEDIUM)",
  },
  C: {
    cls: "bg-confidence-low-bg text-confidence-low",
    long: "Class C — Triangulation support (gap-fill / scaling only)",
  },
};

const ACCESS_LABEL: Record<string, string> = {
  api: "API",
  scrape: "Scrape",
  web_search: "Web search",
  manual_upload: "Manual upload",
};

function TierBadge({ tier }: { tier: MethodTier | null | undefined }) {
  if (!tier) return <span className="text-ink-subtle">—</span>;
  const s = TIER_STYLE[tier];
  return (
    <span className={`badge text-2xs font-semibold ${s.cls}`} title={s.title}>
      {tier}
    </span>
  );
}

function SourceClassBadge({ cls }: { cls: SourceClass | null | undefined }) {
  if (!cls) return null;
  const s = CLASS_STYLE[cls];
  return (
    <span
      className={`badge text-2xs font-semibold ${s.cls}`}
      title={s.long}
      aria-label={s.long}
    >
      Class {cls}
    </span>
  );
}

function AccessBadge({ method }: { method: string | null | undefined }) {
  if (!method) return null;
  return (
    <span className="badge text-2xs bg-surface-subtle text-ink-muted">
      {ACCESS_LABEL[method] ?? method}
    </span>
  );
}

// ---------------------------------------------------------------------------
// SourcePanel — inline disclosure expanded by clicking an estimate row
// ---------------------------------------------------------------------------

interface SourcePanelProps {
  row: CellTriangulationView;
  cellId: number;
}

function SourcePanel({ row, cellId }: SourcePanelProps) {
  /**
   * Raw payload endpoint contract (backend must implement):
   *   GET /cells/{cellId}/triangulation/{triangulationId}/raw
   *   Returns: the verbatim raw_*.raw_json JSONB row that fed this estimate.
   */
  const rawPayloadUrl = `${API_BASE_URL}/cells/${cellId}/triangulation/${row.triangulation_id}/raw`;

  return (
    <div
      className="rounded-lg border border-brand-100 bg-white p-5 shadow-card"
      role="region"
      aria-label={`Source details for ${row.source_publisher}`}
    >
      {/* Panel header */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-ink">Source</h3>
        <SourceClassBadge cls={row.source_class} />
        <AccessBadge method={row.source_access_method} />
      </div>

      {/* Source detail grid */}
      <dl className="grid grid-cols-1 gap-x-8 gap-y-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
        <div>
          <dt className="eyebrow">Publisher</dt>
          <dd className="mt-0.5 font-medium text-ink">{row.source_publisher}</dd>
        </div>

        <div>
          <dt className="eyebrow">Source ID</dt>
          <dd className="mt-0.5 font-mono text-xs text-ink">{row.source_id}</dd>
        </div>

        <div>
          <dt className="eyebrow">Accessed</dt>
          <dd className="mt-0.5 text-ink" title={row.computed_at}>
            {formatTimestamp(row.computed_at)}
          </dd>
        </div>

        {row.source_url && (
          <div className="sm:col-span-2 lg:col-span-3">
            <dt className="eyebrow">URL</dt>
            <dd className="mt-0.5 break-all">
              <DrillLink external href={row.source_url} variant="muted">
                {row.source_url}
              </DrillLink>
            </dd>
          </div>
        )}
      </dl>

      {/* Divider + raw payload CTA (click 2 in the audit chain) */}
      <div className="mt-4 flex flex-wrap items-center gap-4 border-t border-line pt-4">
        <DrillLink external href={rawPayloadUrl} variant="primary">
          View raw payload
        </DrillLink>
        <span className="text-xs text-ink-subtle">
          Full JSONB record stored from{" "}
          <span className="font-mono">{row.source_id}</span>
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// EstimatesPanel (exported) — summary bar + accordion table
// ---------------------------------------------------------------------------

export interface EstimatesPanelProps {
  triangulations: CellTriangulationView[];
  cellId: number;
}

export function EstimatesPanel({ triangulations, cellId }: EstimatesPanelProps) {
  const [expandedId, setExpandedId] = useState<number | null>(null);

  function toggleRow(id: number) {
    setExpandedId((prev) => (prev === id ? null : id));
  }

  // Sort: Tier A → B → C; within tier by estimate descending.
  const sorted = useMemo<CellTriangulationView[]>(() => {
    const tierOrder: Record<MethodTier, number> = { A: 0, B: 1, C: 2 };
    return [...triangulations].sort((a, b) => {
      const at = tierOrder[a.method_tier as MethodTier] ?? 2;
      const bt = tierOrder[b.method_tier as MethodTier] ?? 2;
      if (at !== bt) return at - bt;
      return (b.estimate_usd_m ?? 0) - (a.estimate_usd_m ?? 0);
    });
  }, [triangulations]);

  // Summary stats derived from the triangulation array.
  const stats = useMemo(() => {
    const distinctMethods = new Set(triangulations.map((t) => t.method_code)).size;
    const distinctSourceClasses = new Set(
      triangulations.map((t) => t.source_class).filter(Boolean),
    ).size;
    const hasTierA = triangulations.some((t) => t.method_tier === "A");

    const estimates = triangulations
      .map((t) => t.estimate_usd_m)
      .filter((v): v is number => v != null)
      .sort((a, b) => a - b);

    const min = estimates[0] ?? null;
    const max = estimates[estimates.length - 1] ?? null;
    const median =
      estimates.length > 0 ? estimates[Math.floor(estimates.length / 2)] : null;
    const spreadRatio =
      median != null && max != null && min != null && median > 0
        ? (max - min) / median
        : null;

    return {
      n: triangulations.length,
      distinctMethods,
      distinctSourceClasses,
      hasTierA,
      spreadRatio,
    };
  }, [triangulations]);

  // Empty state
  if (triangulations.length === 0) {
    return (
      <section aria-labelledby="estimates-heading">
        <h2 id="estimates-heading" className="mb-4 text-base font-semibold text-ink">
          Triangulation Estimates
        </h2>
        <div className="card flex flex-col items-center justify-center gap-2 py-14 text-center">
          <p className="text-sm font-medium text-ink">No estimates yet</p>
          <p className="max-w-sm text-xs text-ink-muted">
            The pipeline has not produced any triangulation results for this cell.
            Run the pipeline or check the{" "}
            <DrillLink href="/status" variant="muted">
              Status page
            </DrillLink>{" "}
            for connector health.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section aria-labelledby="estimates-heading">
      {/* Section header + summary bar */}
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="estimates-heading" className="text-base font-semibold text-ink">
            Triangulation Estimates
          </h2>
          <p className="mt-0.5 text-xs text-ink-muted">
            Click a row to inspect the source — then "View raw payload" to
            reach the verbatim data record.
          </p>
        </div>

        {/* Summary chips */}
        <div
          className="flex flex-wrap items-center gap-2 text-xs text-ink-muted"
          aria-label="Triangulation summary"
        >
          <span>
            <span className="font-semibold text-ink">{stats.n}</span> estimate
            {stats.n !== 1 ? "s" : ""}
          </span>
          <span aria-hidden>·</span>
          <span>
            <span className="font-semibold text-ink">{stats.distinctMethods}</span>{" "}
            method{stats.distinctMethods !== 1 ? "s" : ""}
          </span>
          <span aria-hidden>·</span>
          <span>
            <span className="font-semibold text-ink">
              {stats.distinctSourceClasses}
            </span>{" "}
            source class
            {stats.distinctSourceClasses !== 1 ? "es" : ""}
          </span>
          {stats.spreadRatio != null && (
            <>
              <span aria-hidden>·</span>
              <span title="(max − min) / median">
                <span className="font-semibold text-ink">
                  {formatSpread(stats.spreadRatio)}
                </span>{" "}
                spread
              </span>
            </>
          )}
          {stats.hasTierA && (
            <>
              <span aria-hidden>·</span>
              <span
                className="rounded bg-confidence-high-bg px-1.5 py-0.5 font-medium text-confidence-high"
                title="At least one Tier A (primary structured) estimate is present"
              >
                Tier A present
              </span>
            </>
          )}
        </div>
      </div>

      {/* Estimates accordion table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-line bg-surface-subtle">
                <th
                  scope="col"
                  className="eyebrow whitespace-nowrap px-4 py-2.5 text-left"
                >
                  Method
                </th>
                <th
                  scope="col"
                  className="eyebrow whitespace-nowrap px-3 py-2.5 text-left"
                >
                  Tier
                </th>
                <th
                  scope="col"
                  className="eyebrow whitespace-nowrap px-4 py-2.5 text-right"
                >
                  Estimate (USD M)
                </th>
                <th
                  scope="col"
                  className="eyebrow whitespace-nowrap px-4 py-2.5 text-left"
                >
                  Source
                </th>
                <th
                  scope="col"
                  className="eyebrow whitespace-nowrap px-4 py-2.5 text-left"
                >
                  Notes
                </th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((row) => {
                const isOpen = expandedId === row.triangulation_id;
                return (
                  <Fragment key={row.triangulation_id}>
                    {/* Estimate row — click 1 of the audit chain */}
                    <tr
                      onClick={() => toggleRow(row.triangulation_id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          toggleRow(row.triangulation_id);
                        }
                      }}
                      tabIndex={0}
                      role="button"
                      aria-expanded={isOpen}
                      aria-controls={`source-panel-${row.triangulation_id}`}
                      className={`cursor-pointer border-b border-line transition-colors hover:bg-surface-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-inset ${
                        isOpen ? "bg-brand-50" : ""
                      }`}
                    >
                      {/* Method column */}
                      <td className="px-4 py-3 align-middle">
                        <div className="font-mono text-xs font-medium text-ink">
                          {row.method_code}
                        </div>
                        {row.method_description && (
                          <div className="mt-0.5 text-xs text-ink-muted">
                            {row.method_description}
                          </div>
                        )}
                      </td>

                      {/* Tier column */}
                      <td className="px-3 py-3 align-middle">
                        <TierBadge tier={row.method_tier} />
                      </td>

                      {/* Estimate column — tabular numbers, right-aligned */}
                      <td className="tnum px-4 py-3 text-right align-middle">
                        <span className="text-base font-semibold text-ink">
                          {formatUsdMillions(row.estimate_usd_m)}
                        </span>
                      </td>

                      {/* Source column */}
                      <td className="px-4 py-3 align-middle">
                        <span className="text-ink">{row.source_publisher}</span>
                        {row.source_class && (
                          <span
                            className={`ml-2 badge text-2xs font-semibold ${
                              CLASS_STYLE[row.source_class]?.cls ?? ""
                            }`}
                          >
                            {row.source_class}
                          </span>
                        )}
                      </td>

                      {/* Notes column */}
                      <td className="max-w-xs truncate px-4 py-3 align-middle text-ink-muted">
                        {row.notes ?? (
                          <span className="text-ink-subtle">—</span>
                        )}
                      </td>
                    </tr>

                    {/* Source panel row — expanded inline (click 1 result) */}
                    {isOpen && (
                      <tr
                        id={`source-panel-${row.triangulation_id}`}
                        className="border-b border-line bg-brand-50/40"
                      >
                        <td
                          colSpan={5}
                          className="px-6 py-4"
                          // Stop click from bubbling to the parent row and toggling closed.
                          onClick={(e) => e.stopPropagation()}
                        >
                          <SourcePanel row={row} cellId={cellId} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
