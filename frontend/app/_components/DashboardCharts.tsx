"use client";

/**
 * DashboardCharts — three Recharts visualisations for the dashboard landing.
 *
 * Client component: Recharts needs the browser DOM. Data is fetched server-side
 * in page.tsx and passed as a single ``overview`` prop so SSR is fast and
 * hydration is instant.
 *
 * Charts:
 *   1. Confidence-distribution donut (PieChart, innerRadius) — by cell count.
 *   2. Market size by product family (horizontal BarChart) — TAM in $M.
 *   3. Market size by geography (vertical BarChart) — country-level aggregate.
 *
 * Every chart carries a title, subtitle, and legend/labels.
 * TAM values are formatted via formatUsdMillions from lib/format.ts.
 * Colours mirror the design tokens in tailwind.config.ts.
 */

import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RechartsTooltip,
  Legend,
  PieChart,
  Pie,
  Cell as RechartsCell,
  ResponsiveContainer,
} from "recharts";
import { formatUsdMillions } from "@/lib/format";
import type { StatsOverview } from "@/lib/api";

// ---------------------------------------------------------------------------
// Design tokens (mirrors tailwind.config.ts hex values exactly)
// ---------------------------------------------------------------------------

const COLOR = {
  high: "#059669",   // emerald-600 — confidence.high
  medium: "#d97706", // amber-600   — confidence.medium
  low: "#64748b",    // slate-500   — confidence.low
};

/**
 * Seven palette colours for product families — brand-adjacent, distinct enough
 * for up to 7 families in the Medtech APAC taxonomy.
 */
const FAMILY_PALETTE = [
  "#E1198B", // GRX10 magenta (brand)
  "#0891b2", // cyan-600
  "#7c3aed", // violet-600
  "#059669", // emerald-600
  "#d97706", // amber-600
  "#dc2626", // red-600
  "#0d9488", // teal-600
];

/** Per-country fill for the geography bar chart. */
function geoColor(country: string): string {
  if (country === "China") return "#E1198B";
  if (country === "Malaysia") return "#0891b2";
  if (country === "Singapore") return "#7c3aed";
  return "#64748b";
}

// ---------------------------------------------------------------------------
// Custom Recharts tooltip wrapper
// ---------------------------------------------------------------------------

function TamTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: Array<{ value: number; name: string }>;
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-line bg-surface px-3 py-2 text-xs shadow-raised">
      {label && <div className="mb-1 font-semibold text-ink">{label}</div>}
      {payload.map((p) => (
        <div key={p.name} className="text-ink-muted">
          {p.name}: <span className="font-medium text-ink">{formatUsdMillions(p.value)}</span>
        </div>
      ))}
    </div>
  );
}

function CountTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ value: number; name: string; payload: { name: string } }>;
}) {
  if (!active || !payload?.length) return null;
  const entry = payload[0];
  return (
    <div className="rounded-lg border border-line bg-surface px-3 py-2 text-xs shadow-raised">
      <div className="font-semibold text-ink">{entry.payload.name}</div>
      <div className="text-ink-muted">
        {entry.value} cells
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export interface DashboardChartsProps {
  overview: StatsOverview;
}

export default function DashboardCharts({ overview }: DashboardChartsProps) {
  const { confidence_breakdown: cb, by_family, by_geography } = overview;

  // ── 1. Confidence donut data ─────────────────────────────────────────────
  const pieData = [
    { name: "High", value: cb.high.count, color: COLOR.high },
    { name: "Medium", value: cb.medium.count, color: COLOR.medium },
    { name: "Low", value: cb.low.count, color: COLOR.low },
  ];

  // ── 2. Family bar data (already ordered by TAM desc from backend) ────────
  const familyData = by_family.map((r) => ({
    ...r,
    // Truncate long family names for the Y-axis label
    shortName: r.family.length > 28 ? `${r.family.slice(0, 26)}…` : r.family,
  }));

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
      {/* ── Confidence distribution donut ─────────────────────────────── */}
      <section className="card flex flex-col p-5">
        <h2 className="text-sm font-semibold text-ink">
          Confidence distribution
        </h2>
        <p className="mt-0.5 text-xs text-ink-subtle">
          By cell count · {overview.cell_count} cells total
        </p>

        {/* Mini legend above the chart */}
        <div className="mt-3 flex flex-wrap gap-3">
          {pieData.map((d) => (
            <span key={d.name} className="flex items-center gap-1.5 text-xs text-ink-muted">
              <span
                className="inline-block h-2.5 w-2.5 rounded-full"
                style={{ backgroundColor: d.color }}
              />
              {d.name}: {d.value}
            </span>
          ))}
        </div>

        {/* Fixed-size PieChart (no ResponsiveContainer) — the donut hit a
            ResponsiveContainer zero-measure race in the narrow column and
            rendered blank; a fixed size always paints. */}
        <div className="mt-3 flex flex-1 items-center justify-center" style={{ minHeight: 220 }}>
          <PieChart width={260} height={220}>
            <Pie
              data={pieData}
              cx={130}
              cy={110}
              innerRadius={58}
              outerRadius={88}
              paddingAngle={3}
              dataKey="value"
              isAnimationActive={false}
              label={({ name, percent }) =>
                `${name} ${(percent * 100).toFixed(0)}%`
              }
              labelLine
            >
              {pieData.map((entry) => (
                <RechartsCell key={entry.name} fill={entry.color} />
              ))}
            </Pie>
            <RechartsTooltip content={<CountTooltip />} />
          </PieChart>
        </div>
      </section>

      {/* ── Market size by family (horizontal bar) ───────────────────────── */}
      <section className="card flex flex-col p-5 lg:col-span-2">
        <h2 className="text-sm font-semibold text-ink">
          Market size by product family · {overview.year}
        </h2>
        <p className="mt-0.5 text-xs text-ink-subtle">
          TAM in USD · all geographies combined
        </p>
        <div className="mt-4 flex-1" style={{ minHeight: 260 }}>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart
              data={familyData}
              layout="vertical"
              margin={{ top: 0, right: 60, left: 8, bottom: 0 }}
            >
              <CartesianGrid strokeDasharray="3 3" horizontal={false} />
              <XAxis
                type="number"
                tickFormatter={(v: number) => formatUsdMillions(v)}
                tick={{ fontSize: 11, fill: "#64748b" }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                type="category"
                dataKey="shortName"
                width={175}
                tick={{ fontSize: 11, fill: "#475569" }}
                axisLine={false}
                tickLine={false}
              />
              <RechartsTooltip content={<TamTooltip />} />
              <Bar dataKey="tam_usd_m" name="TAM" radius={[0, 4, 4, 0]} isAnimationActive={false}>
                {familyData.map((entry, i) => (
                  <RechartsCell
                    key={entry.family}
                    fill={FAMILY_PALETTE[i % FAMILY_PALETTE.length]}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      {/* ── Market size by geography ─────────────────────────────────────── */}
      <section className="card flex flex-col p-5 lg:col-span-3">
        <h2 className="text-sm font-semibold text-ink">
          Market size by geography · {overview.year}
        </h2>
        <p className="mt-0.5 text-xs text-ink-subtle">
          TAM in USD · all product families combined
        </p>

        {/* Inline legend */}
        <div className="mt-3 flex flex-wrap gap-4">
          {by_geography.map((g) => (
            <span key={g.country} className="flex items-center gap-1.5 text-xs text-ink-muted">
              <span
                className="inline-block h-2.5 w-2.5 rounded-sm"
                style={{ backgroundColor: geoColor(g.country) }}
              />
              {g.country}: {formatUsdMillions(g.tam_usd_m)} ({g.share.toFixed(1)}%)
            </span>
          ))}
        </div>

        <div className="mt-4 flex-1" style={{ minHeight: 200 }}>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart
              data={by_geography}
              margin={{ top: 0, right: 30, left: 20, bottom: 0 }}
            >
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="country"
                tick={{ fontSize: 13, fill: "#374151", fontWeight: 500 }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tickFormatter={(v: number) => formatUsdMillions(v)}
                tick={{ fontSize: 11, fill: "#64748b" }}
                axisLine={false}
                tickLine={false}
              />
              <RechartsTooltip content={<TamTooltip />} />
              <Bar dataKey="tam_usd_m" name="TAM" radius={[4, 4, 0, 0]} isAnimationActive={false}>
                {by_geography.map((entry) => (
                  <RechartsCell
                    key={entry.country}
                    fill={geoColor(entry.country)}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>
    </div>
  );
}
