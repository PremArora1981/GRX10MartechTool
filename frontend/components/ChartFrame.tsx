import type { ReactNode } from "react";
import type { Confidence, Segment } from "@/lib/types";
import { ConfidenceChip } from "./ConfidenceChip";
import { SegmentBadge } from "./SegmentBadge";

/**
 * ChartFrame — the mandatory wrapper for EVERY chart in the app.
 *
 * Acceptance criterion (spec §9 / v1-definition §6): "every chart shows segment
 * + confidence chip". Recharts/visx have no notion of our domain metadata, so
 * screen agents MUST wrap their chart in <ChartFrame segment=… confidence=…>.
 * The frame renders a titled header carrying the segment badge + confidence
 * chip and a consistent card surface; the chart itself goes in `children`.
 *
 * `segment` and `confidence` are required (not optional) on purpose — it should
 * be impossible to ship a chart without them.
 */

export interface ChartFrameProps {
  title: string;
  /** Trade-direction segment the chart depicts (first-class dimension). */
  segment: Segment;
  /** Programmatic confidence of the underlying numbers. */
  confidence: Confidence | null;
  /** Optional country prefix for the segment badge. */
  country?: string;
  /** Distinct method count, surfaced on the confidence chip. */
  methodCount?: number;
  /** Optional supporting subtitle (e.g. units, year range). */
  subtitle?: ReactNode;
  /** Top-right slot for chart-specific controls (toggles, legend). */
  actions?: ReactNode;
  /** Fixed height for the chart area; charts use ResponsiveContainer inside. */
  height?: number;
  /** The Recharts/visx chart. */
  children: ReactNode;
  className?: string;
}

export function ChartFrame({
  title,
  segment,
  confidence,
  country,
  methodCount,
  subtitle,
  actions,
  height = 280,
  children,
  className = "",
}: ChartFrameProps) {
  return (
    <figure className={`card flex flex-col p-4 ${className}`}>
      <figcaption className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-ink">{title}</h3>
          {subtitle && (
            <p className="mt-0.5 text-xs text-ink-muted">{subtitle}</p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <SegmentBadge segment={segment} country={country} size="sm" />
            <ConfidenceChip
              confidence={confidence}
              methodCount={methodCount}
              size="sm"
            />
          </div>
        </div>
        {actions && <div className="shrink-0">{actions}</div>}
      </figcaption>
      <div style={{ height }} className="min-h-0 w-full">
        {children}
      </div>
    </figure>
  );
}

export default ChartFrame;
