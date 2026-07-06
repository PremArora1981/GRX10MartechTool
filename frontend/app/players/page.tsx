export const dynamic = "force-dynamic";
/**
 * Players screen — Layer 3.
 *
 * Server Component: reads the `cell_id` search-param, fetches all data in
 * parallel, then passes it to the client-only PlayersView (Recharts + tables).
 * The CellSelector drives navigation by pushing URL search-param updates.
 *
 * Acceptance criteria (spec §9 / v1-definition §6):
 *  - Every chart wrapped in <ChartFrame segment= confidence=> (enforced by ChartFrame).
 *  - Every share row carries a non-null source_id surfaced as a DrillLink.
 *  - Sub-second list views (server-fetched; 60 s Next.js cache default).
 */

import type { Metadata } from "next";
import { api } from "@/lib/api";
import { PageHeader } from "@/components";
import { CellSelector } from "./_components/CellSelector";
import { DiscoverPlayersButton } from "./_components/DiscoverPlayersButton";
import { PlayersView } from "./_components/PlayersView";
import type { CellView, PlayerShare, SupplierRelationship } from "@/lib/types";

export const metadata: Metadata = { title: "Players" };

interface PageProps {
  /** Next.js App Router injects searchParams for Server Components. */
  searchParams?: { cell_id?: string };
}

export default async function PlayersPage({ searchParams }: PageProps) {
  const rawCellId = searchParams?.cell_id;
  const cellId = rawCellId !== undefined ? parseInt(rawCellId, 10) : NaN;
  const hasCellId = rawCellId !== undefined && !Number.isNaN(cellId);

  // Cell list for the selector dropdown — cached 60 s by the api.ts default.
  let cells: CellView[] = [];
  try {
    const paged = await api.listCells({ limit: 500 });
    cells = paged.items;
  } catch {
    // Backend unreachable: render empty selector gracefully.
  }

  let cell: CellView | null = null;
  let shares: PlayerShare[] = [];
  let relationships: SupplierRelationship[] = [];
  let fetchError: string | null = null;

  if (hasCellId) {
    try {
      [cell, shares, relationships] = await Promise.all([
        api.getCell(cellId),
        api.listPlayerShares(cellId),
        api.listSupplierRelationships(cellId),
      ]);
    } catch (err) {
      fetchError =
        err instanceof Error ? err.message : "Failed to load player data.";
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Layer 3 — Players"
        title="Player Shares"
        description="Top-N producer and supplier shares per market cell. Every share row carries a non-null source — click to drill to the connector and its raw evidence."
        actions={
          <CellSelector
            cells={cells}
            selectedCellId={hasCellId ? cellId : null}
          />
        }
      />

      {!hasCellId && <PlayersEmptyState />}

      {hasCellId && fetchError && <PlayersErrorBanner message={fetchError} />}

      {/* Cell selected but no player shares yet (new vertical) — offer AI discovery. */}
      {hasCellId && !fetchError && cell && shares.length === 0 && (
        <NoPlayersState />
      )}

      {hasCellId && !fetchError && cell && shares.length > 0 && (
        <PlayersView
          cell={cell}
          shares={shares}
          relationships={relationships}
        />
      )}

      {/* Cell ID provided but cell came back null (404 or empty) */}
      {hasCellId && !fetchError && !cell && (
        <div className="card px-4 py-8 text-center text-sm text-ink-muted">
          Cell #{cellId} was not found. Please select a different cell.
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Local server-rendered sub-components
// ---------------------------------------------------------------------------

function PlayersEmptyState() {
  return (
    <div className="flex min-h-[360px] items-center justify-center rounded-card border border-dashed border-line">
      <div className="max-w-sm px-6 py-12 text-center">
        {/* People icon — inline SVG keeps the bundle lean */}
        <svg
          className="mx-auto mb-4 h-10 w-10 text-ink-subtle"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <path d="M16 11a4 4 0 10-8 0" />
          <path d="M2 21a8 8 0 0120 0" />
          <path d="M20 8a3 3 0 11-6 0" />
          <path d="M22 21a6 6 0 00-9-5.2" />
        </svg>
        <h2 className="mb-1 text-sm font-semibold text-ink">No cell selected</h2>
        <p className="mb-4 text-sm text-ink-muted">
          Choose a market cell from the dropdown above to view ranked player
          shares and supplier relationships.
        </p>
        <p className="mb-3 text-sm text-ink-muted">
          New vertical with no players yet? Kick off AI discovery to populate
          top players per segment.
        </p>
        <div className="flex justify-center">
          <DiscoverPlayersButton prominent />
        </div>
      </div>
    </div>
  );
}

function NoPlayersState() {
  return (
    <div className="card px-6 py-10">
      <div className="mx-auto max-w-md text-center">
        {/* People icon — mirrors the empty-state glyph. */}
        <svg
          className="mx-auto mb-4 h-10 w-10 text-ink-subtle"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <path d="M16 11a4 4 0 10-8 0" />
          <path d="M2 21a8 8 0 0120 0" />
          <path d="M20 8a3 3 0 11-6 0" />
          <path d="M22 21a6 6 0 00-9-5.2" />
        </svg>
        <h2 className="mb-1 text-sm font-semibold text-ink">
          No players discovered yet
        </h2>
        <p className="mb-5 text-sm text-ink-muted">
          This cell has no player shares yet. Run AI discovery to find the top
          players per segment for this engagement.
        </p>
        <div className="flex justify-center">
          <DiscoverPlayersButton prominent />
        </div>
      </div>
    </div>
  );
}

function PlayersErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-card border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
      <span className="font-medium">Error loading player data:</span> {message}
    </div>
  );
}
