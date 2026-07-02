/**
 * Presentation helpers shared by every screen. All numeric formatters emit
 * tabular-friendly strings; pair with the `.tnum` utility class for alignment.
 */

import type { Segment } from "./types";

/**
 * Format a USD-millions value the way the spec wants TAM displayed: compact
 * but unambiguous ("$1.24B", "$840M", "$12.0M"). Input is already in millions.
 */
export function formatUsdMillions(value: number | string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  // Postgres NUMERIC columns arrive over JSON as strings — coerce before math.
  const n = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}T`;
  if (abs >= 1_000) return `$${(n / 1_000).toFixed(2)}B`;
  if (abs >= 1) return `$${n.toFixed(1)}M`;
  return `$${(n * 1_000).toFixed(0)}K`;
}

/** Render a low–high band as "$840M – $1.10B"; falls back gracefully on nulls. */
export function formatBand(
  low: number | null | undefined,
  high: number | null | undefined,
): string {
  if (low == null && high == null) return "—";
  return `${formatUsdMillions(low ?? null)} – ${formatUsdMillions(high ?? null)}`;
}

/** Percent with one decimal: 0.1234 (ratio) -> "12.3%". Pass isRatio=false for 12.34. */
export function formatPct(
  value: number | string | null | undefined,
  isRatio = false,
): string {
  if (value === null || value === undefined) return "—";
  const v = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(v)) return "—";
  const pct = isRatio ? v * 100 : v;
  return `${pct.toFixed(1)}%`;
}

/** Compact integer ("1,240,000" -> "1.24M"). Used for tam_units. */
export function formatCount(value: number | string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const v = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(v)) return "—";
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(v);
}

/** Spread ratio (max-min)/median -> "5.0%" style precision percentage. */
export function formatSpread(value: number | string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const v = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

/** Absolute-then-relative timestamp: "Jun 24, 2026 · 3d ago". */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const date = d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
  return `${date} · ${relativeTime(d)}`;
}

/** "3d ago" / "in 2h" relative-time string. */
export function relativeTime(date: Date, now: Date = new Date()): string {
  const diffMs = date.getTime() - now.getTime();
  const abs = Math.abs(diffMs);
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["year", 31_536_000_000],
    ["month", 2_592_000_000],
    ["day", 86_400_000],
    ["hour", 3_600_000],
    ["minute", 60_000],
  ];
  const rtf = new Intl.RelativeTimeFormat("en-US", { numeric: "auto" });
  for (const [unit, ms] of units) {
    if (abs >= ms) return rtf.format(Math.round(diffMs / ms), unit);
  }
  return "just now";
}

/** Human label for a (country, segment) geography pair: "Japan · Domestic". */
export function geographyLabel(country: string, segment: Segment): string {
  return `${country} · ${segmentLabel(segment)}`;
}

/** Title-case a SCREAMING_SNAKE segment: "SELF_CONSUME" -> "Self consume". */
export function segmentLabel(segment: Segment): string {
  return segment
    .toLowerCase()
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}
