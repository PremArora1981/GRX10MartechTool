export const dynamic = "force-dynamic";
/**
 * Cell Detail page — screen 3 of v1 (v1-definition.md §5).
 *
 * The two-click audit chain (acceptance criterion, v1-definition §6):
 *   cell header (TAM + band + confidence chip)
 *     → click estimate row  → source panel (publisher, class, URL, accessed_at)
 *       → click raw payload → verbatim JSONB record
 *
 * This file is a Next.js App Router Server Component. It fetches both the cell
 * summary and its full triangulation list in parallel, then renders:
 *   - A static header card (TAM, band bar, confidence, segment, meta).
 *   - <EstimatesPanel> — a client component that owns the accordion interaction.
 *
 * Invariants preserved from the spec:
 *   - Every number shown has a drillable source (no orphan figures).
 *   - Confidence is read from the API (computed by the summary view), never set here.
 *   - TAM always displays with its low–high band (TamBand with showBar=true).
 *   - Confidence chip always shows on the same screen as the TAM number.
 */

import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { formatCount, formatTimestamp, geographyLabel } from "@/lib/format";
import {
  ConfidenceChip,
  DrillLink,
  SegmentBadge,
  TamBand,
} from "@/components";
import { EstimatesPanel } from "./EstimatesPanel";

// ---------------------------------------------------------------------------
// Type helpers
// ---------------------------------------------------------------------------

type Props = { params: { cellId: string } };

function parseCellId(raw: string): number | null {
  const n = parseInt(raw, 10);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/**
 * Fetch cell + triangulation in parallel. Surfaces 404 via Next.js notFound()
 * so the error boundary shows a proper not-found page. All other errors bubble
 * to the nearest error.tsx boundary.
 */
async function loadCellData(cellId: number) {
  try {
    const [cell, triangulations] = await Promise.all([
      api.getCell(cellId),
      api.getCellTriangulation(cellId),
    ]);
    return { cell, triangulations };
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) notFound();
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Dynamic metadata
// ---------------------------------------------------------------------------

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const cellId = parseCellId(params.cellId);
  if (cellId == null) return { title: "Cell Detail" };
  try {
    const cell = await api.getCell(cellId);
    return {
      title: `${cell.subcategory_name} · ${cell.country} · ${cell.year}`,
      description: `Market sizing cell: ${geographyLabel(cell.country, cell.segment)} — ${cell.year}. TAM and triangulation estimates.`,
    };
  } catch {
    return { title: "Cell Detail" };
  }
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default async function CellDetailPage({ params }: Props) {
  const cellId = parseCellId(params.cellId);
  if (cellId == null) notFound();

  // Parallel fetch — triangulation may be empty (cell unsized), which is valid.
  const { cell, triangulations } = await loadCellData(cellId);

  return (
    <div className="space-y-6">
      {/* Back breadcrumb */}
      <nav aria-label="Breadcrumb" className="text-sm">
        <DrillLink href="/cells" variant="muted">
          Cell Explorer
        </DrillLink>
      </nav>

      {/* ================================================================
          TAM header card
          Acceptance criterion: "every TAM shows its band" + confidence chip
          ================================================================ */}
      <section className="card p-6" aria-label="Cell summary">
        <div className="flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
          {/* Left column — identity */}
          <div className="min-w-0">
            {/* Eyebrow: family name */}
            <div className="eyebrow mb-1">{cell.family_name}</div>

            {/* Page heading: subcategory name */}
            <h1 className="text-2xl font-semibold leading-tight tracking-tight text-ink">
              {cell.subcategory_name}
            </h1>

            {/* Segment badge + year + confidence chip inline */}
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <SegmentBadge
                segment={cell.segment}
                country={cell.country}
              />
              <span className="text-sm font-medium text-ink-muted">
                {cell.year}
              </span>
              <ConfidenceChip
                confidence={cell.confidence}
                methodCount={cell.n_distinct_methods}
              />
            </div>

            {/* Confidence rationale (if the summary view wrote one) */}
            {cell.confidence_rationale && (
              <p className="mt-3 max-w-xl text-sm text-ink-muted">
                {cell.confidence_rationale}
              </p>
            )}
          </div>

          {/* Right column — TAM + band (the primary number) */}
          <div className="shrink-0 rounded-xl bg-surface-subtle px-6 py-4 sm:text-right">
            <div className="eyebrow mb-2">Total Addressable Market</div>
            <TamBand
              value={cell.tam_revenue_usd_m}
              low={cell.tam_low_usd_m}
              high={cell.tam_high_usd_m}
              size="lg"
              showBar
            />
            {cell.tam_units != null && (
              <div className="mt-2 text-xs text-ink-muted">
                {formatCount(cell.tam_units)} units
              </div>
            )}
          </div>
        </div>

        {/* Meta footer — secondary identifiers */}
        <dl className="mt-5 flex flex-wrap gap-x-6 gap-y-1.5 border-t border-line pt-4 text-xs">
          <div className="flex items-center gap-1.5 text-ink-muted">
            <dt className="font-medium text-ink">Cell</dt>
            <dd>#{cell.cell_id}</dd>
          </div>
          <div className="flex items-center gap-1.5 text-ink-muted">
            <dt className="font-medium text-ink">Status</dt>
            <dd className="capitalize">{cell.status}</dd>
          </div>
          <div className="flex items-center gap-1.5 text-ink-muted">
            <dt className="font-medium text-ink">Last updated</dt>
            <dd>{formatTimestamp(cell.updated_at)}</dd>
          </div>
          <div className="flex items-center gap-1.5 text-ink-muted">
            <dt className="font-medium text-ink">Geography</dt>
            <dd>{geographyLabel(cell.country, cell.segment)}</dd>
          </div>
        </dl>
      </section>

      {/* ================================================================
          Estimates accordion table (client component — interactive)
          Click 1: row → source panel
          Click 2: source panel → raw payload
          ================================================================ */}
      <EstimatesPanel triangulations={triangulations} cellId={cellId} />
    </div>
  );
}
