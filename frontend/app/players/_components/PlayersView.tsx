"use client";

/**
 * PlayersView — interactive client layer for the Players screen.
 *
 * Receives server-fetched data as props (no SWR / client fetch needed; RSC
 * page.tsx handles data loading). Renders:
 *
 *   1. Cell summary strip — TAM+band, confidence chip, segment badge.
 *   2. Role filter tabs — client-side filter over player_role values.
 *   3. Recharts horizontal BarChart (Top-N shares with error-bar bands) wrapped
 *      in <ChartFrame> to satisfy the acceptance criterion "every chart shows
 *      segment + confidence chip".
 *   4. DataTable — all share rows for the selected role with source DrillLinks.
 *   5. DataTable — supplier relationships with evidence-strength badges.
 *
 * Invariant: every share row shown in the table has a non-null source_id from
 * the DB constraint; this is surfaced as a DrillLink to /connectors#<source_id>.
 */

import { useMemo, useState } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RechartsTooltip,
  ErrorBar,
  ResponsiveContainer,
  Cell as RechartsCell,
} from "recharts";
import type { TooltipProps } from "recharts";
import {
  ChartFrame,
  ConfidenceChip,
  DataTable,
  DrillLink,
  SegmentBadge,
  TamBand,
} from "@/components";
import type { Column } from "@/components";
import type {
  CellView,
  Confidence,
  PlayerShare,
  SupplierRelationship,
} from "@/lib/types";
import { formatPct, formatUsdMillions, geographyLabel } from "@/lib/format";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Maximum number of bars in the chart. */
const TOP_N = 10;

/**
 * Bar fill colours keyed to the share's confidence level.
 * Mirrors design-token palette from tailwind.config.ts.
 */
const CONFIDENCE_BAR_COLOR: Record<string, string> = {
  high: "#059669",   // emerald-600 (confidence.high)
  medium: "#d97706", // amber-600   (confidence.medium)
  low: "#94a3b8",    // slate-400   (confidence.low — intentionally muted)
};
const DEFAULT_BAR_COLOR = "#E1198B"; // GRX10 magenta when confidence is null

// ---------------------------------------------------------------------------
// Local sub-components
// ---------------------------------------------------------------------------

/** Badge for supplier-relationship evidence strength. */
function EvidenceStrengthBadge({ strength }: { strength: string }) {
  const normalized = strength.toLowerCase();
  const cls =
    normalized === "strong"
      ? "bg-confidence-high-bg text-confidence-high"
      : normalized === "moderate"
        ? "bg-confidence-medium-bg text-confidence-medium"
        : "bg-confidence-low-bg text-confidence-low";
  return (
    <span className={`badge ${cls} capitalize`}>{strength}</span>
  );
}

// ---------------------------------------------------------------------------
// Chart data shape
// ---------------------------------------------------------------------------

interface ChartRow {
  /** Truncated label shown on the Y-axis. */
  name: string;
  /** Full company name shown in the tooltip. */
  fullName: string;
  /** Point estimate (share_pct). Used as Bar dataKey. */
  share: number;
  /** Lower error arm: share_pct − share_low_pct. */
  errorLow: number;
  /** Upper error arm: share_high_pct − share_pct. */
  errorHigh: number;
  /**
   * [lower, upper] tuple consumed by Recharts ErrorBar.
   * Recharts interprets an array dataKey value as [lowerBound, upperBound].
   */
  errorValues: [number, number];
  /** Raw player_role (used in tooltip). */
  role: string;
  /** Raw confidence value (used for bar colour). */
  confidence: string | null;
  /** source_id for the tooltip footnote. */
  source_id: string;
  /** Pre-computed fill colour for the Cell override. */
  color: string;
}

// ---------------------------------------------------------------------------
// Custom Recharts tooltip
// ---------------------------------------------------------------------------

function ShareTooltip({ active, payload }: TooltipProps<number, string>) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload as ChartRow;
  const bandLow = d.share - d.errorLow;
  const bandHigh = d.share + d.errorHigh;
  const hasBand = d.errorLow > 0 || d.errorHigh > 0;

  return (
    <div className="card px-3 py-2.5 text-xs shadow-raised">
      <p className="mb-1 font-semibold text-ink">{d.fullName}</p>
      <p className="tnum text-ink">
        {formatPct(d.share)} market share
      </p>
      {hasBand && (
        <p className="tnum text-ink-muted">
          Band: {formatPct(bandLow)} – {formatPct(bandHigh)}
        </p>
      )}
      <p className="mt-1 capitalize text-ink-muted">{d.role}</p>
      {d.confidence && (
        <p className="capitalize text-ink-subtle">
          Confidence: {d.confidence}
        </p>
      )}
      <p className="text-ink-subtle">Source: {d.source_id}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export interface PlayersViewProps {
  cell: CellView;
  shares: PlayerShare[];
  relationships: SupplierRelationship[];
}

export function PlayersView({ cell, shares, relationships }: PlayersViewProps) {
  // ------- Role filter ----------------------------------------------------- //
  const roles = useMemo(() => {
    const seen = new Set<string>();
    for (const s of shares) seen.add(s.player_role);
    return ["all", ...Array.from(seen).sort()];
  }, [shares]);

  const [selectedRole, setSelectedRole] = useState<string>("all");

  const filteredShares = useMemo(
    () =>
      selectedRole === "all"
        ? shares
        : shares.filter((s) => s.player_role === selectedRole),
    [shares, selectedRole],
  );

  // ------- Top-N for chart ------------------------------------------------- //
  const topShares = useMemo(
    () => [...filteredShares].sort((a, b) => a.rank - b.rank).slice(0, TOP_N),
    [filteredShares],
  );

  const chartData: ChartRow[] = useMemo(
    () =>
      topShares.map((s) => {
        const share = s.share_pct ?? 0;
        const errorLow =
          s.share_pct != null && s.share_low_pct != null
            ? Math.max(0, s.share_pct - s.share_low_pct)
            : 0;
        const errorHigh =
          s.share_pct != null && s.share_high_pct != null
            ? Math.max(0, s.share_high_pct - s.share_pct)
            : 0;
        const nameTrunc =
          s.company_name.length > 18
            ? s.company_name.slice(0, 17) + "…"
            : s.company_name;
        return {
          name: nameTrunc,
          fullName: s.company_name,
          share,
          errorLow,
          errorHigh,
          errorValues: [errorLow, errorHigh],
          role: s.player_role,
          confidence: s.confidence,
          source_id: s.source_id,
          color:
            CONFIDENCE_BAR_COLOR[s.confidence?.toLowerCase() ?? ""] ??
            DEFAULT_BAR_COLOR,
        };
      }),
    [topShares],
  );

  // Adaptive chart height: 40 px per bar, min 180 px, max 520 px.
  const chartHeight = Math.min(520, Math.max(180, topShares.length * 40));

  // ------- DataTable columns — player shares -------------------------------- //
  const shareColumns: Column<PlayerShare>[] = useMemo(
    () => [
      {
        key: "rank",
        header: "#",
        render: (r) => <span className="tnum text-ink-muted">{r.rank}</span>,
        align: "right",
        sortValue: (r) => r.rank,
        width: "3.5rem",
      },
      {
        key: "company",
        header: "Company",
        render: (r) => (
          <span className="font-medium text-ink">{r.company_name}</span>
        ),
        sortValue: (r) => r.company_name,
      },
      {
        key: "role",
        header: "Role",
        render: (r) => (
          <span className="badge bg-surface-subtle capitalize text-ink-muted">
            {r.player_role}
          </span>
        ),
        sortValue: (r) => r.player_role,
        width: "8rem",
      },
      {
        key: "share",
        header: "Share %",
        render: (r) => (
          <span className="tnum">{formatPct(r.share_pct)}</span>
        ),
        align: "right",
        sortValue: (r) => r.share_pct,
        width: "6rem",
      },
      {
        key: "band",
        header: "Low – High",
        render: (r) => (
          <span className="tnum text-ink-muted">
            {formatPct(r.share_low_pct)} – {formatPct(r.share_high_pct)}
          </span>
        ),
        align: "right",
        width: "9rem",
      },
      {
        key: "revenue",
        header: "Revenue",
        render: (r) => (
          <span className="tnum">{formatUsdMillions(r.revenue_usd_m)}</span>
        ),
        align: "right",
        sortValue: (r) => r.revenue_usd_m,
        width: "7rem",
      },
      {
        key: "confidence",
        header: "Confidence",
        render: (r) => (
          <ConfidenceChip
            confidence={r.confidence as Confidence | null}
            size="sm"
          />
        ),
        sortValue: (r) => r.confidence,
        width: "8rem",
      },
      {
        key: "source",
        header: "Source",
        render: (r) => (
          <DrillLink
            href={`/connectors#${encodeURIComponent(r.source_id)}`}
            variant="muted"
          >
            {r.source_id}
          </DrillLink>
        ),
      },
    ],
    [],
  );

  // ------- DataTable columns — supplier relationships ----------------------- //
  const relColumns: Column<SupplierRelationship>[] = useMemo(
    () => [
      {
        key: "buyer",
        header: "Buyer",
        render: (r) => (
          <span className="font-medium text-ink">{r.buyer_name}</span>
        ),
        sortValue: (r) => r.buyer_name,
      },
      {
        key: "supplier",
        header: "Supplier",
        render: (r) => (
          <span className="font-medium text-ink">{r.supplier_name}</span>
        ),
        sortValue: (r) => r.supplier_name,
      },
      {
        key: "type",
        header: "Relationship",
        render: (r) => (
          <span className="badge bg-surface-subtle capitalize text-ink-muted">
            {r.relationship_type}
          </span>
        ),
        sortValue: (r) => r.relationship_type,
        width: "9rem",
      },
      {
        key: "evidence_type",
        header: "Evidence",
        render: (r) => (
          <span className="capitalize text-ink-muted">{r.evidence_type}</span>
        ),
        sortValue: (r) => r.evidence_type,
      },
      {
        key: "strength",
        header: "Strength",
        render: (r) => (
          <EvidenceStrengthBadge strength={r.evidence_strength} />
        ),
        sortValue: (r) => r.evidence_strength,
        width: "7rem",
      },
      {
        key: "source",
        header: "Source",
        render: (r) => (
          <DrillLink
            href={`/connectors#${encodeURIComponent(r.source_id)}`}
            variant="muted"
          >
            {r.source_id}
          </DrillLink>
        ),
      },
    ],
    [],
  );

  // ------- Render ---------------------------------------------------------- //

  return (
    <div className="space-y-6">
      {/* ── 1. Cell summary strip ── */}
      <div className="card flex flex-wrap items-center gap-4 px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="eyebrow mb-0.5">
            {cell.family_name ? `${cell.family_name} / ` : ""}
            {cell.subcategory_name}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-ink">
              {geographyLabel(cell.country, cell.segment)} · {cell.year}
            </span>
            <SegmentBadge
              segment={cell.segment}
              country={cell.country}
              size="sm"
            />
            <ConfidenceChip
              confidence={cell.confidence}
              methodCount={cell.n_distinct_methods}
              size="sm"
            />
          </div>
        </div>
        <TamBand
          value={cell.tam_revenue_usd_m}
          low={cell.tam_low_usd_m}
          high={cell.tam_high_usd_m}
          showBar
          size="sm"
          className="text-right"
        />
      </div>

      {/* ── 2. Role filter tabs ── */}
      {roles.length > 2 && (
        <div
          className="flex flex-wrap gap-1"
          role="tablist"
          aria-label="Filter players by role"
        >
          {roles.map((role) => (
            <button
              key={role}
              role="tab"
              aria-selected={selectedRole === role}
              onClick={() => setSelectedRole(role)}
              className={`focusable rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                selectedRole === role
                  ? "bg-brand text-white"
                  : "border border-line bg-surface text-ink-muted hover:bg-surface-subtle hover:text-ink"
              }`}
            >
              {role === "all"
                ? "All Roles"
                : role.charAt(0).toUpperCase() + role.slice(1)}
            </button>
          ))}
        </div>
      )}

      {/* ── 3. Chart + share table ── */}
      {filteredShares.length === 0 ? (
        <div className="card px-4 py-10 text-center text-sm text-ink-subtle">
          {selectedRole !== "all"
            ? `No player shares recorded for role "${selectedRole}".`
            : "No player shares recorded for this cell."}
        </div>
      ) : (
        <>
          {/* Recharts horizontal bar chart */}
          <ChartFrame
            title={`Top ${Math.min(TOP_N, topShares.length)} Players by Market Share`}
            segment={cell.segment}
            confidence={cell.confidence}
            country={cell.country}
            methodCount={cell.n_distinct_methods}
            subtitle={`${cell.subcategory_name} · ${cell.year} · showing ${topShares.length} of ${filteredShares.length} player${filteredShares.length !== 1 ? "s" : ""}`}
            height={chartHeight}
          >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={chartData}
                layout="vertical"
                margin={{ top: 4, right: 40, left: 8, bottom: 4 }}
              >
                <CartesianGrid
                  horizontal={false}
                  stroke="var(--line)"
                  strokeDasharray="3 3"
                />
                <XAxis
                  type="number"
                  domain={[0, "auto"]}
                  tickFormatter={(v: number) => `${v}%`}
                  tick={{ fontSize: 11, fill: "var(--ink-muted)" }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  type="category"
                  dataKey="name"
                  width={130}
                  tick={{ fontSize: 11, fill: "var(--ink)" }}
                  axisLine={false}
                  tickLine={false}
                />
                <RechartsTooltip
                  content={<ShareTooltip />}
                  cursor={{ fill: "var(--surface-subtle)" }}
                />
                <Bar dataKey="share" maxBarSize={28} radius={[0, 4, 4, 0]}>
                  {chartData.map((entry, i) => (
                    <RechartsCell key={`bar-${i}`} fill={entry.color} />
                  ))}
                  {/*
                   * ErrorBar renders the low–high confidence band.
                   * errorValues = [lowerArm, upperArm] where:
                   *   lowerArm = share_pct − share_low_pct
                   *   upperArm = share_high_pct − share_pct
                   * In a layout="vertical" BarChart, Recharts automatically
                   * draws error bars in the X direction.
                   */}
                  <ErrorBar
                    dataKey="errorValues"
                    width={4}
                    strokeWidth={1.5}
                    stroke="var(--ink-subtle)"
                  />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </ChartFrame>

          {/* Share detail table */}
          <section aria-labelledby="shares-heading">
            <h2 id="shares-heading" className="eyebrow mb-3">
              Player Shares · {filteredShares.length}{" "}
              {filteredShares.length === 1 ? "record" : "records"}
            </h2>
            <DataTable
              columns={shareColumns}
              rows={filteredShares}
              rowKey={(r) => r.share_id}
              initialSort={{ key: "rank", dir: "asc" }}
              empty="No player shares for the selected role."
              dense
            />
          </section>
        </>
      )}

      {/* ── 4. Supplier relationships ── */}
      <section aria-labelledby="relationships-heading">
        <h2 id="relationships-heading" className="eyebrow mb-3">
          Supplier Relationships · {relationships.length}{" "}
          {relationships.length === 1 ? "record" : "records"}
        </h2>
        {relationships.length === 0 ? (
          <div className="card px-4 py-8 text-center text-sm text-ink-subtle">
            No supplier relationships recorded for this cell.
          </div>
        ) : (
          <DataTable
            columns={relColumns}
            rows={relationships}
            rowKey={(r) => r.relationship_id}
            initialSort={{ key: "buyer", dir: "asc" }}
            empty="No relationships to display."
            dense
          />
        )}
      </section>
    </div>
  );
}

export default PlayersView;
