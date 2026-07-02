"use client";

import { useMemo, useState, type ReactNode } from "react";

/**
 * Generic, typed data table for list views (Cell Explorer, Connectors, Players,
 * Assumptions, Status). Client component: supports click-to-sort and a sticky
 * header. Cells render via per-column `render` so screens can drop in badges,
 * TamBand, DrillLink, etc. Kept dependency-free for sub-second list views.
 */

export interface Column<T> {
  /** Stable key; also the default sort accessor when `sortValue` is omitted. */
  key: string;
  header: ReactNode;
  /** Cell renderer. */
  render: (row: T, index: number) => ReactNode;
  /** Value used for sorting; omit to disable sorting on this column. */
  sortValue?: (row: T) => string | number | null | undefined;
  align?: "left" | "right" | "center";
  /** Optional fixed width, e.g. "12rem". */
  width?: string;
  className?: string;
  headerClassName?: string;
}

export interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  /** Stable React key per row. */
  rowKey: (row: T, index: number) => string | number;
  /** Optional row click (e.g. navigate to detail). */
  onRowClick?: (row: T) => void;
  /** Initial sort column key + direction. */
  initialSort?: { key: string; dir: "asc" | "desc" };
  /** Shown when `rows` is empty. */
  empty?: ReactNode;
  /** Compact density for dense audit tables. */
  dense?: boolean;
  className?: string;
}

const ALIGN: Record<NonNullable<Column<unknown>["align"]>, string> = {
  left: "text-left",
  right: "text-right",
  center: "text-center",
};

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  initialSort,
  empty = "No rows.",
  dense = false,
  className = "",
}: DataTableProps<T>) {
  const [sort, setSort] = useState<{ key: string; dir: "asc" | "desc" } | null>(
    initialSort ?? null,
  );

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return rows;
    const accessor = col.sortValue;
    const factor = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = accessor(a);
      const bv = accessor(b);
      if (av == null && bv == null) return 0;
      if (av == null) return 1; // nulls last
      if (bv == null) return -1;
      if (typeof av === "number" && typeof bv === "number") {
        return (av - bv) * factor;
      }
      return String(av).localeCompare(String(bv)) * factor;
    });
  }, [rows, sort, columns]);

  function toggleSort(col: Column<T>) {
    if (!col.sortValue) return;
    setSort((prev) => {
      if (prev?.key !== col.key) return { key: col.key, dir: "asc" };
      if (prev.dir === "asc") return { key: col.key, dir: "desc" };
      return null; // third click clears
    });
  }

  const cellPad = dense ? "px-3 py-1.5" : "px-4 py-2.5";

  return (
    <div className={`card overflow-hidden ${className}`}>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-line bg-surface-subtle">
              {columns.map((col) => {
                const sortable = !!col.sortValue;
                const active = sort?.key === col.key;
                return (
                  <th
                    key={col.key}
                    scope="col"
                    style={col.width ? { width: col.width } : undefined}
                    className={`${cellPad} ${ALIGN[col.align ?? "left"]} eyebrow whitespace-nowrap ${
                      sortable ? "cursor-pointer select-none hover:text-ink" : ""
                    } ${col.headerClassName ?? ""}`}
                    onClick={sortable ? () => toggleSort(col) : undefined}
                    aria-sort={
                      active
                        ? sort!.dir === "asc"
                          ? "ascending"
                          : "descending"
                        : undefined
                    }
                  >
                    <span className="inline-flex items-center gap-1">
                      {col.header}
                      {sortable && (
                        <span aria-hidden className="text-ink-subtle">
                          {active ? (sort!.dir === "asc" ? "▲" : "▼") : "⇅"}
                        </span>
                      )}
                    </span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 ? (
              <tr>
                <td
                  colSpan={columns.length}
                  className="px-4 py-10 text-center text-ink-subtle"
                >
                  {empty}
                </td>
              </tr>
            ) : (
              sorted.map((row, i) => (
                <tr
                  key={rowKey(row, i)}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={`border-b border-line last:border-0 ${
                    onRowClick
                      ? "cursor-pointer transition-colors hover:bg-surface-subtle"
                      : ""
                  }`}
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={`${cellPad} ${ALIGN[col.align ?? "left"]} align-middle ${col.className ?? ""}`}
                    >
                      {col.render(row, i)}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default DataTable;
