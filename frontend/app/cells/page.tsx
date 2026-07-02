/**
 * Cell Explorer — server-component page entry (Next.js 14 App Router RSC).
 *
 * Architecture: this is a React Server Component. It reads the current URL
 * search-params to build an initial `CellQuery`, then fetches the first page
 * of cells plus the filter-option lists (subcategories, geographies) in one
 * parallel server-side round-trip. The pre-fetched data is handed to
 * `CellsClient` as `initialPage`, which uses it as SWR `fallbackData` so the
 * table renders on first paint with no loading spinner.
 *
 * Any filter change in the client pushes new URL params → SWR key changes →
 * SWR refetches while keeping previous data visible (sub-second feel,
 * acceptance criterion). The URL also makes every filtered view bookmarkable
 * and server-renderable with the correct initial data.
 *
 * Error handling: every API call catches gracefully — a non-running backend
 * yields empty lists rather than a 500. The page still renders; the user sees
 * zero rows and the filter bar is ready for when data arrives.
 */

import type { Metadata } from "next";
import { Suspense } from "react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import type { Confidence } from "@/lib/types";
import CellsClient from "./CellsClient";

export const metadata: Metadata = { title: "Cell Explorer" };

// ---------------------------------------------------------------------------
// Search-param helpers
// ---------------------------------------------------------------------------

type SP = Record<string, string | string[] | undefined>;

/** Coerce a search-param (possibly an array from Next.js catchall) to a single
 *  string, returning undefined when absent. */
function str(sp: SP, key: string): string | undefined {
  const v = sp[key];
  return Array.isArray(v) ? v[0] : v || undefined;
}

/** Parse a search-param as a positive finite integer, or return undefined. */
function posInt(sp: SP, key: string): number | undefined {
  const s = str(sp, key);
  if (!s) return undefined;
  const n = Number(s);
  return Number.isInteger(n) && n > 0 ? n : undefined;
}

/** The cells per page — must match the limit sent to the server and the client. */
const PAGE_SIZE = 25;

// ---------------------------------------------------------------------------
// Page component
// ---------------------------------------------------------------------------

interface Props {
  searchParams: SP;
}

export default async function CellExplorerPage({ searchParams }: Props) {
  // Build the initial API query from URL params so the server render matches
  // what the client will hydrate.
  const page = Math.max(1, posInt(searchParams, "page") ?? 1);
  const rawConfidence = str(searchParams, "confidence");
  const confidence =
    rawConfidence === "high" || rawConfidence === "medium" || rawConfidence === "low"
      ? (rawConfidence as Confidence)
      : undefined;

  // Parallel server-side fetches — all three resolve in one network round-trip
  // (sub-second first-paint requirement).
  const [initialPage, subcategories, geographies] = await Promise.all([
    api
      .listCells({
        subcategory_id: posInt(searchParams, "subcategory_id"),
        geography_id: posInt(searchParams, "geography_id"),
        year: posInt(searchParams, "year"),
        confidence,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      })
      .catch(() => ({ items: [], total: 0, limit: PAGE_SIZE, offset: 0 })),

    api.listSubcategories().catch(() => []),
    api.listGeographies().catch(() => []),
  ]);

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Sizing"
        title="Cell Explorer"
        description={
          <>
            Browse market cells (subcategory &times; geography &times; year). Each row
            shows the TAM band, confidence chip, and segment — click any row to open the
            two-click triangulation audit chain.
          </>
        }
      />

      {/*
        Suspense is required here because CellsClient calls `useSearchParams`,
        which opts the component into dynamic rendering on the client. Wrapping it
        in Suspense prevents the "Missing Suspense boundary" build error.
        The fallback is invisible (zero height) because the table content is
        already hydrated from `initialPage` — no visible loading flash.
      */}
      <Suspense fallback={null}>
        <CellsClient
          initialPage={initialPage}
          subcategories={subcategories}
          geographies={geographies}
          pageSize={PAGE_SIZE}
        />
      </Suspense>
    </div>
  );
}
