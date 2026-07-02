import { formatBand, formatUsdMillions } from "@/lib/format";

/**
 * TAM display with its low–high band (acceptance criterion: "every TAM shows
 * its band"). Renders a primary point estimate plus the band beneath, and an
 * optional inline visual bar positioning the point within [low, high].
 *
 * All inputs are USD-millions, matching `cells.tam_*_usd_m`.
 */

export interface TamBandProps {
  value: number | null | undefined;
  low: number | null | undefined;
  high: number | null | undefined;
  /** Render the small low|point|high range bar. */
  showBar?: boolean;
  size?: "sm" | "md" | "lg";
  className?: string;
}

const VALUE_SIZE: Record<NonNullable<TamBandProps["size"]>, string> = {
  sm: "text-base",
  md: "text-xl",
  lg: "text-3xl",
};

export function TamBand({
  value,
  low,
  high,
  showBar = false,
  size = "md",
  className = "",
}: TamBandProps) {
  const hasBand = low != null && high != null;
  // Position of the point estimate within the band, clamped to [0,1].
  let pointPct: number | null = null;
  if (hasBand && value != null && high! > low!) {
    pointPct = Math.min(1, Math.max(0, (value - low!) / (high! - low!)));
  }

  return (
    <div className={className}>
      <div className={`tnum font-semibold leading-tight ${VALUE_SIZE[size]}`}>
        {formatUsdMillions(value)}
      </div>
      <div className="tnum mt-0.5 text-xs text-ink-muted">
        {formatBand(low, high)}
      </div>
      {showBar && hasBand && (
        <div className="mt-2">
          <div className="relative h-1.5 w-full rounded-full bg-confidence-low-bg">
            <div className="absolute inset-y-0 left-0 right-0 rounded-full bg-brand-100" />
            {pointPct !== null && (
              <div
                className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-brand shadow"
                style={{ left: `${pointPct * 100}%` }}
                aria-hidden
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default TamBand;
