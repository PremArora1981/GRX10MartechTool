import type { Confidence } from "@/lib/types";

/**
 * Confidence chip (Q5). Confidence is computed programmatically by the
 * `cell_triangulation_summary` view — this component only renders it, it never
 * lets a user set it. Three states map to the design-token confidence colours.
 *
 * Acceptance criterion: every TAM/chart shows a confidence chip, so this is the
 * single canonical renderer — screen agents must reuse it, not re-roll badges.
 */

const STYLES: Record<
  Confidence,
  { label: string; cls: string; dot: string }
> = {
  high: {
    label: "High",
    cls: "bg-confidence-high-bg text-confidence-high",
    dot: "bg-confidence-high",
  },
  medium: {
    label: "Medium",
    cls: "bg-confidence-medium-bg text-confidence-medium",
    dot: "bg-confidence-medium",
  },
  low: {
    label: "Low",
    cls: "bg-confidence-low-bg text-confidence-low",
    dot: "bg-confidence-low",
  },
};

export interface ConfidenceChipProps {
  confidence: Confidence | null | undefined;
  /** Show the number of distinct methods that triangulated (e.g. "· 3 methods"). */
  methodCount?: number;
  size?: "sm" | "md";
  className?: string;
}

export function ConfidenceChip({
  confidence,
  methodCount,
  size = "md",
  className = "",
}: ConfidenceChipProps) {
  if (!confidence) {
    return (
      <span
        className={`badge bg-confidence-low-bg text-ink-subtle ${className}`}
        title="Not yet triangulated"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-ink-subtle" aria-hidden />
        Unsized
      </span>
    );
  }
  const s = STYLES[confidence];
  const pad = size === "sm" ? "px-1.5 py-0 text-2xs" : "";
  return (
    <span
      className={`badge ${s.cls} ${pad} ${className}`}
      title={`${s.label} confidence`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${s.dot}`} aria-hidden />
      {s.label}
      {methodCount !== undefined && (
        <span className="font-normal opacity-70">· {methodCount} methods</span>
      )}
    </span>
  );
}

export default ConfidenceChip;
