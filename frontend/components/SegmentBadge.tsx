import type { Segment } from "@/lib/types";
import { segmentLabel } from "@/lib/format";

/**
 * Trade-direction / segment badge. Segmentation is a first-class dimension in
 * the spec, and "every chart shows segment + confidence chip" — so this is the
 * canonical segment renderer reused by Cell Explorer, charts, and player views.
 *
 * Known segments get a dedicated colour; unknown values degrade to neutral.
 */

const KNOWN: Record<string, { cls: string; dot: string }> = {
  DOMESTIC: { cls: "bg-segment-domestic-bg text-segment-domestic", dot: "bg-segment-domestic" },
  IMPORT: { cls: "bg-segment-import-bg text-segment-import", dot: "bg-segment-import" },
  EXPORT: { cls: "bg-segment-export-bg text-segment-export", dot: "bg-segment-export" },
  SELF_CONSUME: { cls: "bg-segment-self-bg text-segment-self", dot: "bg-segment-self" },
};

const FALLBACK = {
  cls: "bg-segment-other-bg text-segment-other",
  dot: "bg-segment-other",
};

export interface SegmentBadgeProps {
  segment: Segment;
  /** Optional country prefix: "Japan · Domestic". */
  country?: string;
  size?: "sm" | "md";
  className?: string;
}

export function SegmentBadge({
  segment,
  country,
  size = "md",
  className = "",
}: SegmentBadgeProps) {
  const style = KNOWN[String(segment).toUpperCase()] ?? FALLBACK;
  const pad = size === "sm" ? "px-1.5 py-0 text-2xs" : "";
  return (
    <span className={`badge ${style.cls} ${pad} ${className}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} aria-hidden />
      {country ? `${country} · ` : ""}
      {segmentLabel(segment)}
    </span>
  );
}

export default SegmentBadge;
