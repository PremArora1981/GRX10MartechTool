"use client";

/**
 * CellsClient — interactive filter bar + SWR-powered data table for the Cell Explorer.
 *
 * Design contract:
 *   - Filter state lives in URL search-params so every filtered view is bookmarkable
 *     and the server pre-render matches what the client hydrates.
 *   - SWR key is derived from the URL search-params string → stable string key, no
 *     reference-equality churn.
 *   - `keepPreviousData: true` keeps the last result visible while a refetch runs
 *     (acceptance criterion: sub-second feel; no blank flash between filter changes).
 *   - initialPage (server-fetched) is SWR fallbackData so the first paint is data-loaded.
 *
 * The component intentionally avoids managing its own numeric page state outside the URL
 * so the browser back/forward buttons navigate filter history correctly.
 */

import { useCallback, useMemo, useTransition } from "react";
import type { ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useApi } from "@/lib/swr";
import { segmentLabel } from "@/lib/format";
import {
  ConfidenceChip,
  DataTable,
  DrillLink,
  SegmentBadge,
  TamBand,
} from "@/components";
import type { Column } from "@/components/DataTable";
import type {
  CellView,
  Confidence,
  Geography,
  Paginated,
  TaxonomySubcategory,
} from "@/lib/types";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Row shape for the Cell Explorer table.
 *
 * The established `CellView` contract declares `family_name` and
 * `n_distinct_methods` as non-optional, but the current backend `CellSummary`
 * response omits them. We widen those two fields to optional/nullable so the
 * column renderers can degrade gracefully without TypeScript errors, while
 * keeping every other `CellView` field at its declared type.
 */
type RowData = Omit<CellView, "family_name" | "n_distinct_methods"> & {
  family_name?: string | null;
  n_distinct_methods?: number | null;
};

interface CellsClientProps {
  /** First page of cells pre-fetched on the server. Used as SWR fallbackData. */
  initialPage: Paginated<CellView>;
  /** Full subcategory list for the filter dropdown. */
  subcategories: TaxonomySubcategory[];
  /** Full geography list for the filter dropdown. */
  geographies: Geography[];
  /** Number of rows per page (must match server-side PAGE_SIZE). */
  pageSize: number;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CONFIDENCE_OPTIONS: { label: string; value: Confidence | "" }[] = [
  { label: "All confidence", value: "" },
  { label: "High", value: "high" },
  { label: "Medium", value: "medium" },
  { label: "Low", value: "low" },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build a stable SWR path from the current URL search-param string.
 * Encoding the query into the path ensures the SWR key is a simple string —
 * no reference-equality problems with object keys.
 */
function buildSwrPath(params: URLSearchParams): string {
  const p = new URLSearchParams();
  const copy = (key: string) => {
    const v = params.get(key);
    if (v) p.set(key, v);
  };
  copy("subcategory_id");
  copy("geography_id");
  copy("year");
  copy("confidence");

  const page = Math.max(1, Number(params.get("page") ?? "1") || 1);
  p.set("limit", "25");
  p.set("offset", String((page - 1) * 25));

  return `/cells?${p.toString()}`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/** A small labelled select used in the filter bar. */
function FilterSelect({
  id,
  label,
  value,
  onChange,
  children,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="eyebrow">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md border border-line bg-surface px-3 py-1.5 text-sm text-ink
                   shadow-sm transition-colors focus:border-brand focus:outline-none
                   focus:ring-1 focus:ring-brand"
      >
        {children}
      </select>
    </div>
  );
}

/** Pagination row: "Showing X–Y of Z" + Prev/Next. */
function PaginationBar({
  total,
  page,
  pageSize,
  onPage,
  isLoading,
}: {
  total: number;
  page: number;
  pageSize: number;
  onPage: (p: number) => void;
  isLoading: boolean;
}) {
  if (total === 0) return null;
  const totalPages = Math.ceil(total / pageSize);
  const from = Math.min((page - 1) * pageSize + 1, total);
  const to = Math.min(page * pageSize, total);

  return (
    <div className="flex items-center justify-between py-1 text-sm text-ink-muted">
      <span>
        Showing{" "}
        <span className="tnum font-medium text-ink">
          {from}–{to}
        </span>{" "}
        of{" "}
        <span className="tnum font-medium text-ink">{total}</span> cells
      </span>
      <div className="flex gap-2">
        <button
          onClick={() => onPage(page - 1)}
          disabled={page <= 1 || isLoading}
          aria-label="Previous page"
          className="rounded-md border border-line px-3 py-1.5 text-sm font-medium
                     transition-colors hover:bg-surface-subtle disabled:opacity-40
                     disabled:cursor-not-allowed focusable"
        >
          ← Prev
        </button>
        <span className="flex items-center px-2 text-ink-subtle">
          {page} / {totalPages}
        </span>
        <button
          onClick={() => onPage(page + 1)}
          disabled={page >= totalPages || isLoading}
          aria-label="Next page"
          className="rounded-md border border-line px-3 py-1.5 text-sm font-medium
                     transition-colors hover:bg-surface-subtle disabled:opacity-40
                     disabled:cursor-not-allowed focusable"
        >
          Next →
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function CellsClient({
  initialPage,
  subcategories,
  geographies,
  pageSize,
}: CellsClientProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [isPending, startTransition] = useTransition();

  // ── Derived filter values from URL params ──────────────────────────────────
  const currentSubcategoryId = searchParams.get("subcategory_id") ?? "";
  const currentGeographyId = searchParams.get("geography_id") ?? "";
  const currentYear = searchParams.get("year") ?? "";
  const currentConfidence = searchParams.get("confidence") ?? "";
  const currentPage = Math.max(1, Number(searchParams.get("page") ?? "1") || 1);

  // ── SWR ───────────────────────────────────────────────────────────────────
  const swrPath = useMemo(() => buildSwrPath(searchParams), [searchParams]);
  const { data, isLoading } = useApi<Paginated<RowData>>(swrPath, undefined, {
    fallbackData: initialPage as Paginated<RowData>,
    keepPreviousData: true,
    // No revalidateOnFocus — these are analytical list views; avoid surprising
    // mid-analysis refreshes. Users can reload manually.
    revalidateOnFocus: false,
  });

  const page = data ?? (initialPage as Paginated<RowData>);
  const rows = page.items;

  // ── Param mutation helpers ─────────────────────────────────────────────────
  const setParam = useCallback(
    (key: string, value: string) => {
      const p = new URLSearchParams(searchParams.toString());
      if (value) {
        p.set(key, value);
      } else {
        p.delete(key);
      }
      // Any filter change resets to page 1.
      if (key !== "page") p.delete("page");
      startTransition(() => {
        router.push(`?${p.toString()}`, { scroll: false });
      });
    },
    [router, searchParams],
  );

  const clearFilters = useCallback(() => {
    startTransition(() => {
      router.push("?", { scroll: false });
    });
  }, [router]);

  const hasFilters =
    currentSubcategoryId ||
    currentGeographyId ||
    currentYear ||
    currentConfidence;

  // ── Table columns ──────────────────────────────────────────────────────────
  const columns: Column<RowData>[] = useMemo(
    () => [
      {
        key: "subcategory",
        header: "Subcategory",
        render: (row) => (
          <div className="min-w-0">
            {row.family_name && (
              <div className="mb-0.5 truncate text-2xs text-ink-subtle">
                {row.family_name}
              </div>
            )}
            <div className="truncate font-medium text-ink">
              {row.subcategory_name || "—"}
            </div>
          </div>
        ),
        sortValue: (row) => row.subcategory_name ?? "",
      },
      {
        key: "geography",
        header: "Geography",
        render: (row) =>
          row.segment ? (
            <SegmentBadge
              segment={row.segment}
              country={row.country ?? undefined}
              size="sm"
            />
          ) : (
            <span className="text-ink-subtle">—</span>
          ),
        sortValue: (row) =>
          row.country && row.segment
            ? `${row.country} ${row.segment}`
            : "",
        width: "14rem",
      },
      {
        key: "year",
        header: "Year",
        render: (row) => (
          <span className="tnum text-ink">{row.year}</span>
        ),
        sortValue: (row) => row.year,
        align: "center",
        width: "5rem",
      },
      {
        key: "tam",
        header: "TAM (USD)",
        render: (row) => (
          <TamBand
            value={row.tam_revenue_usd_m}
            low={row.tam_low_usd_m}
            high={row.tam_high_usd_m}
            size="sm"
          />
        ),
        sortValue: (row) => row.tam_revenue_usd_m ?? -1,
        align: "right",
        width: "12rem",
      },
      {
        key: "confidence",
        header: "Confidence",
        render: (row) => (
          <ConfidenceChip
            confidence={row.confidence}
            methodCount={
              row.n_distinct_methods != null && row.n_distinct_methods > 0
                ? row.n_distinct_methods
                : undefined
            }
            size="sm"
          />
        ),
        sortValue: (row) =>
          row.confidence === "high"
            ? 2
            : row.confidence === "medium"
              ? 1
              : row.confidence === "low"
                ? 0
                : -1,
        width: "10rem",
      },
      {
        key: "detail",
        header: "",
        render: (row) => (
          <DrillLink
            href={`/cells/${row.cell_id}`}
            variant="muted"
            prefetch={false}
          >
            Detail
          </DrillLink>
        ),
        width: "6rem",
        align: "right",
      },
    ],
    [],
  );

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-4">
      {/* ── Filter bar ─────────────────────────────────────────────────── */}
      <div className="card p-4">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {/* Subcategory */}
          <FilterSelect
            id="filter-subcategory"
            label="Subcategory"
            value={currentSubcategoryId}
            onChange={(v) => setParam("subcategory_id", v)}
          >
            <option value="">All subcategories</option>
            {subcategories
              .filter((s) => !s.superseded_by)
              .sort((a, b) => a.name.localeCompare(b.name))
              .map((s) => (
                <option key={s.subcategory_id} value={String(s.subcategory_id)}>
                  {s.name}
                </option>
              ))}
          </FilterSelect>

          {/* Geography */}
          <FilterSelect
            id="filter-geography"
            label="Geography"
            value={currentGeographyId}
            onChange={(v) => setParam("geography_id", v)}
          >
            <option value="">All geographies</option>
            {geographies
              .slice()
              .sort((a, b) =>
                a.country.localeCompare(b.country) ||
                a.segment.localeCompare(b.segment),
              )
              .map((g) => (
                <option key={g.geography_id} value={String(g.geography_id)}>
                  {g.country} · {segmentLabel(g.segment)}
                </option>
              ))}
          </FilterSelect>

          {/* Year */}
          <div className="flex flex-col gap-1">
            <label htmlFor="filter-year" className="eyebrow">
              Year
            </label>
            <input
              id="filter-year"
              type="number"
              min={2000}
              max={2040}
              placeholder="Any"
              value={currentYear}
              onChange={(e) => setParam("year", e.target.value)}
              className="rounded-md border border-line bg-surface px-3 py-1.5 text-sm
                         text-ink shadow-sm transition-colors placeholder:text-ink-subtle
                         focus:border-brand focus:outline-none focus:ring-1
                         focus:ring-brand"
            />
          </div>

          {/* Confidence */}
          <FilterSelect
            id="filter-confidence"
            label="Confidence"
            value={currentConfidence}
            onChange={(v) => setParam("confidence", v)}
          >
            {CONFIDENCE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </FilterSelect>

          {/* Clear button — only visible when at least one filter is active */}
          <div className="flex flex-col justify-end">
            <button
              onClick={clearFilters}
              disabled={!hasFilters}
              className="rounded-md border border-line px-3 py-1.5 text-sm
                         font-medium text-ink-muted transition-colors
                         hover:border-ink-subtle hover:text-ink
                         disabled:opacity-30 disabled:cursor-not-allowed
                         focusable"
            >
              Clear filters
            </button>
          </div>
        </div>
      </div>

      {/* ── Result count + loading indicator ───────────────────────────── */}
      <div className="flex items-center justify-between">
        <p className="text-sm text-ink-muted">
          {page.total === 0 ? (
            "No cells match the current filters."
          ) : (
            <>
              <span className="tnum font-medium text-ink">{page.total}</span>{" "}
              {page.total === 1 ? "cell" : "cells"} found
            </>
          )}
        </p>
        {(isLoading || isPending) && (
          <span className="text-xs text-ink-subtle" role="status" aria-live="polite">
            Loading…
          </span>
        )}
      </div>

      {/* ── Data table ─────────────────────────────────────────────────── */}
      <div
        className={`transition-opacity duration-150 ${
          isLoading || isPending ? "opacity-60" : "opacity-100"
        }`}
        aria-busy={isLoading || isPending}
      >
        <DataTable<RowData>
          columns={columns}
          rows={rows}
          rowKey={(row) => row.cell_id}
          onRowClick={(row) => {
            startTransition(() => {
              router.push(`/cells/${row.cell_id}`);
            });
          }}
          initialSort={{ key: "year", dir: "desc" }}
          empty={
            <div className="space-y-1 text-center">
              <div className="font-medium text-ink">No cells found</div>
              <div className="text-sm text-ink-muted">
                Try adjusting or clearing the filters above.
              </div>
            </div>
          }
        />
      </div>

      {/* ── Pagination ─────────────────────────────────────────────────── */}
      <PaginationBar
        total={page.total}
        page={currentPage}
        pageSize={pageSize}
        onPage={(p) => setParam("page", String(p))}
        isLoading={isLoading || isPending}
      />
    </div>
  );
}
