"use client";

/**
 * CellSelector — client-only dropdown that navigates to ?cell_id=<id>.
 *
 * Uses useRouter (not useSearchParams) so no Suspense boundary is required in
 * the parent Server Component. Cells are grouped by taxonomy family for
 * navigability. Receives the pre-fetched cells list from the RSC page.
 */

import { useRouter } from "next/navigation";
import { useMemo } from "react";
import type { CellView } from "@/lib/types";
import { segmentLabel } from "@/lib/format";

export interface CellSelectorProps {
  cells: CellView[];
  selectedCellId: number | null;
}

export function CellSelector({ cells, selectedCellId }: CellSelectorProps) {
  const router = useRouter();

  /** Group cells by family name for <optgroup> UX. */
  const grouped = useMemo(() => {
    const map = new Map<string, CellView[]>();
    for (const c of cells) {
      const family = c.family_name ?? "Other";
      if (!map.has(family)) map.set(family, []);
      map.get(family)!.push(c);
    }
    // Sort groups alphabetically; within each group sort by subcategory then year.
    return Array.from(map.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([family, items]) => ({
        family,
        items: items.sort((a, b) =>
          a.subcategory_name.localeCompare(b.subcategory_name) ||
          a.year - b.year,
        ),
      }));
  }, [cells]);

  function handleChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const val = e.target.value;
    router.push(val ? `/players?cell_id=${val}` : "/players");
  }

  return (
    <select
      value={selectedCellId ?? ""}
      onChange={handleChange}
      aria-label="Select a market cell"
      className="focusable rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink shadow-card focus:outline-none"
    >
      <option value="">— Select a cell —</option>
      {grouped.map(({ family, items }) => (
        <optgroup key={family} label={family}>
          {items.map((c) => (
            <option key={c.cell_id} value={c.cell_id}>
              {c.subcategory_name} · {c.country} {segmentLabel(c.segment)} ·{" "}
              {c.year}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}

export default CellSelector;
