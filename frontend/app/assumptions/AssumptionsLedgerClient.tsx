"use client";

/**
 * AssumptionsLedgerClient — interactive assumptions ledger.
 *
 * Responsibilities:
 *   - Builds versioned chains from the flat assumptions list (no extra round-trip).
 *   - Renders an expandable table: each row is an active assumption; expanding
 *     opens either a version-history timeline or a live cells drill panel.
 *   - Hosts the add/supersede form (modal), gated by canEdit (analyst+).
 *
 * Data flow: the server component (page.tsx) resolves the full assumptions list
 * and reference lookups via SSR; this client component owns all interactivity
 * and fetches influenced cells on demand via SWR.
 */

import {
  useState,
  useMemo,
  type FormEvent,
} from "react";
import { useRouter } from "next/navigation";
import type {
  Assumption,
  Cell,
  Geography,
  TaxonomySubcategory,
  Company,
  Source,
  AppRole,
  Confidence,
} from "@/lib/types";
import { ConfidenceChip, DrillLink, SegmentBadge } from "@/components";
import { useApi } from "@/lib/swr";
import { api, ApiError } from "@/lib/api";
import { formatTimestamp, geographyLabel } from "@/lib/format";

// ===========================================================================
// Domain types
// ===========================================================================

/** One versioned assumption chain: head (active) + all prior superseded versions. */
interface AssumptionChain {
  /** Most recent assumption — superseded_by is null. */
  active: Assumption;
  /** Older versions, oldest-first. Empty for brand-new (never-superseded) assumptions. */
  history: Assumption[];
}

type OpenPanel = "history" | "cells";

export interface AssumptionsLedgerClientProps {
  assumptions: Assumption[];
  geographies: Geography[];
  subcategories: TaxonomySubcategory[];
  companies: Company[];
  sources: Source[];
  canEdit: boolean;
  userRole: AppRole;
}

// ===========================================================================
// Chain building
// ===========================================================================

/**
 * Groups a flat assumption list into versioned chains anchored at active heads.
 *
 * Algorithm:
 *   1. Build predecessorOf[newer_id] = older_id from all superseded_by links.
 *   2. Find heads (superseded_by = null) — each is the tip of one chain.
 *   3. Trace backwards from each head to collect history (oldest-first).
 *
 * Assumptions not reachable from any head (orphaned superseded rows) are
 * intentionally excluded — they indicate a data integrity issue, not a
 * client-side concern.
 */
function buildChains(assumptions: Assumption[]): AssumptionChain[] {
  const byId = new Map(assumptions.map((a) => [a.assumption_id, a]));

  // predecessorOf[newer_id] = older_id
  const predecessorOf = new Map<number, number>();
  for (const a of assumptions) {
    if (a.superseded_by !== null) {
      predecessorOf.set(a.superseded_by, a.assumption_id);
    }
  }

  const heads = assumptions.filter((a) => a.superseded_by === null);

  return heads.map((head) => {
    const history: Assumption[] = [];
    let current = head;
    while (predecessorOf.has(current.assumption_id)) {
      const prevId = predecessorOf.get(current.assumption_id)!;
      const prev = byId.get(prevId);
      if (!prev) break;
      history.push(prev);
      current = prev;
    }
    history.reverse(); // oldest first
    return { active: head, history };
  });
}

// ===========================================================================
// Scope label
// ===========================================================================

function scopeLabel(
  a: Assumption,
  subcategories: TaxonomySubcategory[],
  geographies: Geography[],
  companies: Company[],
): string {
  const parts: string[] = [];
  if (a.scope_company_id !== null) {
    const c = companies.find((x) => x.company_id === a.scope_company_id);
    if (c) parts.push(c.name);
  }
  if (a.scope_subcategory_id !== null) {
    const s = subcategories.find((x) => x.subcategory_id === a.scope_subcategory_id);
    if (s) parts.push(s.name);
  }
  if (a.scope_geography_id !== null) {
    const g = geographies.find((x) => x.geography_id === a.scope_geography_id);
    if (g) parts.push(geographyLabel(g.country, g.segment));
  }
  return parts.length > 0 ? parts.join(" · ") : "Global";
}

// ===========================================================================
// Cells drill panel
// ===========================================================================

/**
 * Fetches and renders the list of cells influenced by the given assumption.
 * Mounts only when the row is expanded to "cells" — SWR caches the result
 * so subsequent opens do not re-fetch.
 */
function CellsDrillPanel({
  assumptionId,
  geographies,
  subcategories,
}: {
  assumptionId: number;
  geographies: Geography[];
  subcategories: TaxonomySubcategory[];
}) {
  const { data: cells, isLoading, error } = useApi<Cell[]>(
    `/assumptions/${assumptionId}/cells`,
  );

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 py-4 text-sm text-ink-subtle">
        <span
          className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-brand border-t-transparent"
          aria-hidden
        />
        Loading influenced cells…
      </div>
    );
  }

  if (error) {
    return (
      <p className="py-3 text-sm text-red-600">
        {error instanceof ApiError
          ? `Failed to load cells: ${error.message}`
          : "Failed to load influenced cells."}
      </p>
    );
  }

  if (!cells || cells.length === 0) {
    return (
      <p className="py-3 text-sm text-ink-subtle">
        No cells are currently linked to this assumption. Link cells via the
        Cell Detail page.
      </p>
    );
  }

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-line">
          <th className="pb-2 text-left eyebrow pr-4">Subcategory</th>
          <th className="pb-2 text-left eyebrow pr-4">Geography</th>
          <th className="pb-2 text-right eyebrow pr-4">Year</th>
          <th className="pb-2 text-right eyebrow pr-4">TAM</th>
          <th className="pb-2 text-left eyebrow pr-4">Confidence</th>
          <th className="pb-2 text-left eyebrow" />
        </tr>
      </thead>
      <tbody>
        {cells.map((cell) => {
          const sub = subcategories.find(
            (s) => s.subcategory_id === cell.subcategory_id,
          );
          const geo = geographies.find(
            (g) => g.geography_id === cell.geography_id,
          );
          return (
            <tr key={cell.cell_id} className="border-b border-line last:border-0">
              <td className="py-2 pr-4 text-ink">
                {sub?.name ?? `#${cell.subcategory_id}`}
              </td>
              <td className="py-2 pr-4">
                {geo ? (
                  <SegmentBadge
                    segment={geo.segment}
                    country={geo.country}
                    size="sm"
                  />
                ) : (
                  <span className="text-ink-subtle">#{cell.geography_id}</span>
                )}
              </td>
              <td className="py-2 pr-4 text-right tnum text-ink">{cell.year}</td>
              <td className="py-2 pr-4 text-right tnum text-ink-muted">
                {cell.tam_revenue_usd_m != null
                  ? `$${cell.tam_revenue_usd_m.toFixed(1)}M`
                  : "—"}
              </td>
              <td className="py-2 pr-4">
                <ConfidenceChip confidence={cell.confidence} size="sm" />
              </td>
              <td className="py-2">
                <DrillLink href={`/cells/${cell.cell_id}`} variant="muted">
                  Detail
                </DrillLink>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// ===========================================================================
// Version history timeline
// ===========================================================================

function HistoryPanel({
  history,
  subcategories,
  geographies,
  companies,
}: {
  history: Assumption[];
  subcategories: TaxonomySubcategory[];
  geographies: Geography[];
  companies: Company[];
}) {
  if (history.length === 0) {
    return (
      <p className="py-3 text-sm text-ink-subtle">
        First version — no prior history.
      </p>
    );
  }

  return (
    <div className="space-y-0">
      {history.map((a, i) => (
        <div key={a.assumption_id} className="flex gap-3">
          {/* Timeline line + dot */}
          <div className="flex flex-col items-center pt-1">
            <div className="h-3 w-3 rounded-full border-2 border-ink-subtle bg-surface flex-shrink-0" />
            <div className="mt-1 w-px flex-1 bg-line min-h-[1rem]" />
          </div>
          {/* Version card */}
          <div className="pb-4 min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5 mb-1">
              <span className="badge border border-line bg-surface-subtle text-ink-subtle">
                v{i + 1} · Superseded
              </span>
              <span className="text-2xs text-ink-subtle">
                {a.effective_from_year}
                {a.effective_to_year ? `–${a.effective_to_year}` : "–"}
              </span>
              <span className="text-2xs text-ink-subtle">
                Added {formatTimestamp(a.created_at)}
              </span>
            </div>
            <p className="text-sm text-ink-muted line-through decoration-ink-subtle/40">
              {a.assumption_text}
            </p>
            <div className="mt-0.5 flex flex-wrap gap-3">
              {a.numeric_value != null && (
                <span className="text-xs text-ink-subtle tnum">
                  {a.numeric_value} {a.unit ?? ""}
                </span>
              )}
              <span className="text-xs text-ink-subtle">
                Scope: {scopeLabel(a, subcategories, geographies, companies)}
              </span>
              {a.derivation_method && (
                <span className="text-xs text-ink-subtle">
                  Method: {a.derivation_method}
                </span>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ===========================================================================
// Add / supersede modal
// ===========================================================================

interface FormState {
  assumption_text: string;
  numeric_value: string;
  unit: string;
  confidence: string;
  derivation_method: string;
  source_id: string;
  effective_from_year: string;
  effective_to_year: string;
  scope_company_id: string;
  scope_subcategory_id: string;
  scope_geography_id: string;
}

const EMPTY_FORM: FormState = {
  assumption_text: "",
  numeric_value: "",
  unit: "",
  confidence: "",
  derivation_method: "",
  source_id: "",
  effective_from_year: String(new Date().getFullYear()),
  effective_to_year: "",
  scope_company_id: "",
  scope_subcategory_id: "",
  scope_geography_id: "",
};

interface AddAssumptionModalProps {
  geographies: Geography[];
  subcategories: TaxonomySubcategory[];
  companies: Company[];
  sources: Source[];
  /** Non-null when this is a supersede action — form pre-fills scope. */
  supersedingAssumption: Assumption | null;
  onClose: () => void;
  onSuccess: () => void;
}

function AddAssumptionModal({
  geographies,
  subcategories,
  companies,
  sources,
  supersedingAssumption,
  onClose,
  onSuccess,
}: AddAssumptionModalProps) {
  const isSupersede = supersedingAssumption !== null;

  const [form, setForm] = useState<FormState>(() => {
    if (!supersedingAssumption) return EMPTY_FORM;
    // Pre-fill scope so the analyst copies the relevant context
    return {
      ...EMPTY_FORM,
      scope_company_id: supersedingAssumption.scope_company_id
        ? String(supersedingAssumption.scope_company_id)
        : "",
      scope_subcategory_id: supersedingAssumption.scope_subcategory_id
        ? String(supersedingAssumption.scope_subcategory_id)
        : "",
      scope_geography_id: supersedingAssumption.scope_geography_id
        ? String(supersedingAssumption.scope_geography_id)
        : "",
      unit: supersedingAssumption.unit ?? "",
      derivation_method: supersedingAssumption.derivation_method ?? "",
      source_id: supersedingAssumption.source_id ?? "",
    };
  });

  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  function setField(field: keyof FormState, value: string) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    if (!form.assumption_text.trim()) {
      setError("Assumption text is required.");
      return;
    }
    const fromYear = Number(form.effective_from_year);
    if (!form.effective_from_year || isNaN(fromYear)) {
      setError("A valid effective from year is required.");
      return;
    }

    setSubmitting(true);
    try {
      await api.createAssumption({
        assumption_text: form.assumption_text.trim(),
        numeric_value: form.numeric_value !== "" ? Number(form.numeric_value) : null,
        unit: form.unit.trim() || null,
        confidence: (form.confidence as Confidence) || null,
        derivation_method: form.derivation_method.trim() || null,
        source_id: form.source_id || null,
        effective_from_year: fromYear,
        effective_to_year:
          form.effective_to_year !== "" ? Number(form.effective_to_year) : null,
        scope_company_id:
          form.scope_company_id !== "" ? Number(form.scope_company_id) : null,
        scope_subcategory_id:
          form.scope_subcategory_id !== ""
            ? Number(form.scope_subcategory_id)
            : null,
        scope_geography_id:
          form.scope_geography_id !== ""
            ? Number(form.scope_geography_id)
            : null,
        supersedes_id: supersedingAssumption?.assumption_id ?? null,
      });
      onSuccess();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Failed to save assumption. Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    /* Backdrop */
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 pt-12"
      role="dialog"
      aria-modal="true"
      aria-labelledby="modal-heading"
    >
      <div className="card w-full max-w-2xl p-6 shadow-raised">
        {/* Header */}
        <div className="mb-5 flex items-center justify-between">
          <h2
            id="modal-heading"
            className="text-lg font-semibold text-ink"
          >
            {isSupersede ? "Create Superseding Version" : "Add Assumption"}
          </h2>
          <button
            onClick={onClose}
            className="focusable rounded p-1 text-ink-subtle hover:text-ink"
            aria-label="Close"
          >
            <svg className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
              <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
            </svg>
          </button>
        </div>

        {/* Superseding context box */}
        {isSupersede && (
          <div className="mb-4 rounded-lg border border-line bg-surface-subtle p-3 text-sm">
            <p className="font-medium text-ink-muted mb-0.5">
              Superseding assumption #{supersedingAssumption.assumption_id}:
            </p>
            <p className="italic text-ink-muted">
              &ldquo;{supersedingAssumption.assumption_text}&rdquo;
            </p>
            <p className="mt-1.5 text-2xs text-ink-subtle">
              The prior version is preserved and will appear in the history
              chain — it is never deleted or overwritten.
            </p>
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Assumption text */}
          <div>
            <label
              htmlFor="af-text"
              className="mb-1 block text-sm font-medium text-ink"
            >
              Assumption{" "}
              <span className="text-red-500" aria-hidden>
                *
              </span>
            </label>
            <textarea
              id="af-text"
              rows={3}
              required
              value={form.assumption_text}
              onChange={(e) => setField("assumption_text", e.target.value)}
              placeholder="e.g. MLCC market grows at 10% CAGR 2025–2030 based on revised WSTS Q4 data"
              className="focusable w-full rounded-lg border border-line bg-surface p-2.5 text-sm text-ink placeholder-ink-subtle focus:border-brand"
            />
          </div>

          {/* Value + unit */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="af-val" className="mb-1 block text-sm font-medium text-ink">
                Numeric value
              </label>
              <input
                id="af-val"
                type="number"
                step="any"
                value={form.numeric_value}
                onChange={(e) => setField("numeric_value", e.target.value)}
                placeholder="e.g. 10.0"
                className="focusable w-full rounded-lg border border-line bg-surface p-2.5 text-sm text-ink placeholder-ink-subtle focus:border-brand"
              />
            </div>
            <div>
              <label htmlFor="af-unit" className="mb-1 block text-sm font-medium text-ink">
                Unit
              </label>
              <input
                id="af-unit"
                type="text"
                value={form.unit}
                onChange={(e) => setField("unit", e.target.value)}
                placeholder="e.g. % CAGR, USD/M"
                className="focusable w-full rounded-lg border border-line bg-surface p-2.5 text-sm text-ink placeholder-ink-subtle focus:border-brand"
              />
            </div>
          </div>

          {/* Effective years */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label
                htmlFor="af-from"
                className="mb-1 block text-sm font-medium text-ink"
              >
                Effective from year{" "}
                <span className="text-red-500" aria-hidden>
                  *
                </span>
              </label>
              <input
                id="af-from"
                type="number"
                required
                min={2000}
                max={2100}
                value={form.effective_from_year}
                onChange={(e) => setField("effective_from_year", e.target.value)}
                placeholder="2025"
                className="focusable w-full rounded-lg border border-line bg-surface p-2.5 text-sm text-ink placeholder-ink-subtle focus:border-brand"
              />
            </div>
            <div>
              <label htmlFor="af-to" className="mb-1 block text-sm font-medium text-ink">
                Effective to year
              </label>
              <input
                id="af-to"
                type="number"
                min={2000}
                max={2100}
                value={form.effective_to_year}
                onChange={(e) => setField("effective_to_year", e.target.value)}
                placeholder="2030 (blank = open)"
                className="focusable w-full rounded-lg border border-line bg-surface p-2.5 text-sm text-ink placeholder-ink-subtle focus:border-brand"
              />
            </div>
          </div>

          {/* Confidence + derivation method */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="af-conf" className="mb-1 block text-sm font-medium text-ink">
                Confidence
              </label>
              <select
                id="af-conf"
                value={form.confidence}
                onChange={(e) => setField("confidence", e.target.value)}
                className="focusable w-full rounded-lg border border-line bg-surface p-2.5 text-sm text-ink focus:border-brand"
              >
                <option value="">— unset —</option>
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
              </select>
            </div>
            <div>
              <label htmlFor="af-method" className="mb-1 block text-sm font-medium text-ink">
                Derivation method
              </label>
              <input
                id="af-method"
                type="text"
                value={form.derivation_method}
                onChange={(e) => setField("derivation_method", e.target.value)}
                placeholder="e.g. Industry consensus, bottom-up"
                className="focusable w-full rounded-lg border border-line bg-surface p-2.5 text-sm text-ink placeholder-ink-subtle focus:border-brand"
              />
            </div>
          </div>

          {/* Source */}
          <div>
            <label htmlFor="af-src" className="mb-1 block text-sm font-medium text-ink">
              Source
            </label>
            <select
              id="af-src"
              value={form.source_id}
              onChange={(e) => setField("source_id", e.target.value)}
              className="focusable w-full rounded-lg border border-line bg-surface p-2.5 text-sm text-ink focus:border-brand"
            >
              <option value="">— no source —</option>
              {sources.map((s) => (
                <option key={s.source_id} value={s.source_id}>
                  {s.publisher} [{s.source_id}]
                </option>
              ))}
            </select>
          </div>

          {/* Scope */}
          <fieldset className="rounded-lg border border-line p-4">
            <legend className="px-1 text-sm font-medium text-ink">
              Scope{" "}
              <span className="text-2xs font-normal text-ink-subtle">
                (omit for global/engagement-wide)
              </span>
            </legend>
            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
              <div>
                <label htmlFor="af-subcat" className="mb-1 block eyebrow">
                  Subcategory
                </label>
                <select
                  id="af-subcat"
                  value={form.scope_subcategory_id}
                  onChange={(e) =>
                    setField("scope_subcategory_id", e.target.value)
                  }
                  className="focusable w-full rounded-lg border border-line bg-surface p-2.5 text-sm text-ink focus:border-brand"
                >
                  <option value="">All</option>
                  {subcategories.map((s) => (
                    <option
                      key={s.subcategory_id}
                      value={String(s.subcategory_id)}
                    >
                      {s.name}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="af-geo" className="mb-1 block eyebrow">
                  Geography
                </label>
                <select
                  id="af-geo"
                  value={form.scope_geography_id}
                  onChange={(e) =>
                    setField("scope_geography_id", e.target.value)
                  }
                  className="focusable w-full rounded-lg border border-line bg-surface p-2.5 text-sm text-ink focus:border-brand"
                >
                  <option value="">All</option>
                  {geographies.map((g) => (
                    <option key={g.geography_id} value={String(g.geography_id)}>
                      {geographyLabel(g.country, g.segment)}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="af-co" className="mb-1 block eyebrow">
                  Company
                </label>
                <select
                  id="af-co"
                  value={form.scope_company_id}
                  onChange={(e) =>
                    setField("scope_company_id", e.target.value)
                  }
                  className="focusable w-full rounded-lg border border-line bg-surface p-2.5 text-sm text-ink focus:border-brand"
                >
                  <option value="">All</option>
                  {companies.map((c) => (
                    <option key={c.company_id} value={String(c.company_id)}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </fieldset>

          {/* Actions */}
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="focusable rounded-lg border border-line px-4 py-2 text-sm font-medium text-ink-muted hover:text-ink"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="focusable rounded-lg bg-brand px-4 py-2 text-sm font-medium text-brand-fg hover:bg-brand-700 disabled:opacity-60"
            >
              {submitting
                ? "Saving…"
                : isSupersede
                ? "Create Superseding Version"
                : "Add Assumption"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ===========================================================================
// Chain row (renders one full assumption + expansion panels)
// ===========================================================================

interface ChainRowProps {
  chain: AssumptionChain;
  index: number;
  isExpanded: boolean;
  openPanel: OpenPanel | null;
  onTogglePanel: (panel: OpenPanel) => void;
  onSupersede: () => void;
  geographies: Geography[];
  subcategories: TaxonomySubcategory[];
  companies: Company[];
  canEdit: boolean;
}

function ChainRow({
  chain,
  index,
  isExpanded,
  openPanel,
  onTogglePanel,
  onSupersede,
  geographies,
  subcategories,
  companies,
  canEdit,
}: ChainRowProps) {
  const { active, history } = chain;
  const versionNum = history.length + 1;
  const versionLabel = `v${versionNum}`;
  const periodStr = active.effective_to_year
    ? `${active.effective_from_year}–${active.effective_to_year}`
    : `${active.effective_from_year}–`;
  const scope = scopeLabel(active, subcategories, geographies, companies);

  const historyPanelOpen = isExpanded && openPanel === "history";
  const cellsPanelOpen = isExpanded && openPanel === "cells";

  return (
    <>
      {/* ---- Main data row ---- */}
      <tr className="border-b border-line hover:bg-surface-subtle transition-colors">
        {/* Row index */}
        <td className="px-4 py-3 text-right text-2xs text-ink-subtle tnum w-10">
          {index + 1}
        </td>

        {/* Assumption text + scope */}
        <td className="px-4 py-3 max-w-xs">
          <p className="text-sm text-ink line-clamp-2">
            {active.assumption_text}
          </p>
          <p className="mt-0.5 text-2xs text-ink-subtle truncate">{scope}</p>
          {active.source_id && (
            <p className="mt-0.5 text-2xs text-ink-subtle">
              Source: <span className="font-mono">{active.source_id}</span>
            </p>
          )}
        </td>

        {/* Numeric value */}
        <td className="px-4 py-3 text-right tnum">
          {active.numeric_value != null ? (
            <span className="text-sm text-ink">
              {active.numeric_value}
              {active.unit && (
                <span className="ml-1 text-2xs text-ink-subtle">
                  {active.unit}
                </span>
              )}
            </span>
          ) : (
            <span className="text-ink-subtle">—</span>
          )}
        </td>

        {/* Period */}
        <td className="px-4 py-3 text-sm text-ink-muted tnum whitespace-nowrap">
          {periodStr}
        </td>

        {/* Confidence */}
        <td className="px-4 py-3">
          {active.confidence ? (
            <ConfidenceChip
              confidence={active.confidence as Confidence}
              size="sm"
            />
          ) : (
            <span className="text-2xs text-ink-subtle">—</span>
          )}
        </td>

        {/* Version badge */}
        <td className="px-4 py-3">
          {history.length > 0 ? (
            <button
              onClick={() => onTogglePanel("history")}
              aria-expanded={historyPanelOpen}
              className={`focusable badge border ${
                historyPanelOpen
                  ? "border-brand bg-brand-50 text-brand"
                  : "border-line bg-surface-subtle text-ink-muted hover:text-ink"
              } transition-colors`}
            >
              {versionLabel} · {history.length} prior
            </button>
          ) : (
            <span className="badge border border-line bg-surface-subtle text-ink-subtle">
              {versionLabel} · New
            </span>
          )}
        </td>

        {/* Action buttons */}
        <td className="px-4 py-3">
          <div className="flex items-center gap-2">
            <button
              onClick={() => onTogglePanel("cells")}
              aria-expanded={cellsPanelOpen}
              className={`focusable rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                cellsPanelOpen
                  ? "bg-brand text-brand-fg"
                  : "border border-line text-ink-muted hover:text-ink"
              }`}
            >
              Cells
            </button>
            {canEdit && (
              <button
                onClick={onSupersede}
                title="Create a new version that supersedes this assumption"
                className="focusable rounded-md border border-line px-2.5 py-1 text-xs font-medium text-ink-muted hover:text-ink transition-colors"
              >
                Supersede
              </button>
            )}
          </div>
        </td>
      </tr>

      {/* ---- History expansion ---- */}
      {historyPanelOpen && (
        <tr className="border-b border-line bg-surface-subtle">
          <td />
          <td colSpan={6} className="px-4 pb-5 pt-3">
            <p className="eyebrow mb-3">
              Version history — oldest first, active at end
            </p>
            <HistoryPanel
              history={history}
              subcategories={subcategories}
              geographies={geographies}
              companies={companies}
            />
            {/* Active version at timeline end */}
            <div className="flex gap-3 mt-1">
              <div className="flex flex-col items-center pt-1">
                <div className="h-3 w-3 rounded-full bg-brand flex-shrink-0" />
              </div>
              <div className="pb-2 min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5 mb-1">
                  <span className="badge border border-brand-200 bg-brand-50 text-brand">
                    {versionLabel} · Active
                  </span>
                  <span className="text-2xs text-ink-subtle">
                    {active.effective_from_year}
                    {active.effective_to_year
                      ? `–${active.effective_to_year}`
                      : "–"}
                  </span>
                  <span className="text-2xs text-ink-subtle">
                    Added {formatTimestamp(active.created_at)}
                  </span>
                </div>
                <p className="text-sm text-ink">{active.assumption_text}</p>
                {active.numeric_value != null && (
                  <p className="mt-0.5 text-xs tnum text-ink-muted">
                    {active.numeric_value} {active.unit ?? ""}
                  </p>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}

      {/* ---- Cells drill expansion ---- */}
      {cellsPanelOpen && (
        <tr className="border-b border-line bg-surface-subtle">
          <td />
          <td colSpan={6} className="px-4 pb-5 pt-3">
            <p className="eyebrow mb-3">
              Cells influenced by assumption #{active.assumption_id}
            </p>
            <CellsDrillPanel
              assumptionId={active.assumption_id}
              geographies={geographies}
              subcategories={subcategories}
            />
          </td>
        </tr>
      )}
    </>
  );
}

// ===========================================================================
// Main export
// ===========================================================================

export default function AssumptionsLedgerClient({
  assumptions,
  geographies,
  subcategories,
  companies,
  sources,
  canEdit,
}: AssumptionsLedgerClientProps) {
  const router = useRouter();

  // Expansion state: which chain is open + which panel
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [openPanel, setOpenPanel] = useState<OpenPanel | null>(null);

  // Modal state
  const [modalOpen, setModalOpen] = useState(false);
  const [supersedingAssumption, setSupersedingAssumption] =
    useState<Assumption | null>(null);

  // Scope filters
  const [filterSubcatId, setFilterSubcatId] = useState<string>("");
  const [filterGeoId, setFilterGeoId] = useState<string>("");

  // Build chains and apply filters
  const chains = useMemo(() => buildChains(assumptions), [assumptions]);

  const filteredChains = useMemo(() => {
    if (!filterSubcatId && !filterGeoId) return chains;
    return chains.filter((chain) => {
      const a = chain.active;
      if (
        filterSubcatId &&
        String(a.scope_subcategory_id) !== filterSubcatId
      )
        return false;
      if (filterGeoId && String(a.scope_geography_id) !== filterGeoId)
        return false;
      return true;
    });
  }, [chains, filterSubcatId, filterGeoId]);

  // Stats
  const totalVersions = assumptions.length;
  const chainCount = chains.length;
  const withHistory = chains.filter((c) => c.history.length > 0).length;

  function handleTogglePanel(chainActiveId: number, panel: OpenPanel) {
    if (expandedId === chainActiveId && openPanel === panel) {
      // Same row, same panel → collapse
      setExpandedId(null);
      setOpenPanel(null);
    } else {
      setExpandedId(chainActiveId);
      setOpenPanel(panel);
    }
  }

  function handleOpenAdd() {
    setSupersedingAssumption(null);
    setModalOpen(true);
  }

  function handleOpenSupersede(assumption: Assumption) {
    setSupersedingAssumption(assumption);
    setModalOpen(true);
  }

  function handleModalClose() {
    setModalOpen(false);
    setSupersedingAssumption(null);
  }

  function handleModalSuccess() {
    setModalOpen(false);
    setSupersedingAssumption(null);
    // Revalidate RSC data without a full navigation
    router.refresh();
  }

  const hasFilters = filterSubcatId !== "" || filterGeoId !== "";

  return (
    <>
      {/* ---- Summary stats ---- */}
      <div className="mb-6 flex flex-wrap gap-4">
        {(
          [
            { label: "Active assumptions", value: chainCount },
            { label: "Versioned chains", value: withHistory },
            { label: "Total versions", value: totalVersions },
          ] as const
        ).map(({ label, value }) => (
          <div key={label} className="card px-4 py-3 min-w-[10rem]">
            <p className="eyebrow">{label}</p>
            <p className="mt-1 text-2xl font-semibold tnum text-ink">
              {value}
            </p>
          </div>
        ))}
      </div>

      {/* ---- Toolbar: filters + add button ---- */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex flex-1 flex-wrap gap-3">
          <select
            value={filterSubcatId}
            onChange={(e) => setFilterSubcatId(e.target.value)}
            aria-label="Filter by subcategory"
            className="focusable rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink focus:border-brand"
          >
            <option value="">All subcategories</option>
            {subcategories.map((s) => (
              <option key={s.subcategory_id} value={String(s.subcategory_id)}>
                {s.name}
              </option>
            ))}
          </select>

          <select
            value={filterGeoId}
            onChange={(e) => setFilterGeoId(e.target.value)}
            aria-label="Filter by geography"
            className="focusable rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink focus:border-brand"
          >
            <option value="">All geographies</option>
            {geographies.map((g) => (
              <option key={g.geography_id} value={String(g.geography_id)}>
                {geographyLabel(g.country, g.segment)}
              </option>
            ))}
          </select>

          {hasFilters && (
            <button
              onClick={() => {
                setFilterSubcatId("");
                setFilterGeoId("");
              }}
              className="focusable text-sm text-ink-muted underline-offset-2 hover:underline"
            >
              Clear filters
            </button>
          )}
        </div>

        {canEdit && (
          <button
            onClick={handleOpenAdd}
            className="focusable rounded-lg bg-brand px-4 py-2 text-sm font-medium text-brand-fg hover:bg-brand-700"
          >
            + Add Assumption
          </button>
        )}
      </div>

      {/* ---- Main table or empty state ---- */}
      {filteredChains.length === 0 ? (
        <div className="card flex flex-col items-center justify-center py-16 text-center">
          <p className="text-sm font-medium text-ink">
            {chains.length === 0
              ? "No assumptions yet"
              : "No assumptions match the current filters"}
          </p>
          <p className="mt-1 max-w-sm text-sm text-ink-muted">
            {chains.length === 0
              ? canEdit
                ? "Add the first assumption to anchor the market-sizing model."
                : "Assumptions will appear here once analysts add them."
              : "Adjust the subcategory or geography filter above."}
          </p>
          {canEdit && chains.length === 0 && (
            <button
              onClick={handleOpenAdd}
              className="focusable mt-4 rounded-lg bg-brand px-4 py-2 text-sm font-medium text-brand-fg hover:bg-brand-700"
            >
              Add First Assumption
            </button>
          )}
        </div>
      ) : (
        <div className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-line bg-surface-subtle">
                  <th className="px-4 py-2.5 text-right eyebrow w-10">#</th>
                  <th className="px-4 py-2.5 text-left eyebrow">Assumption</th>
                  <th className="px-4 py-2.5 text-right eyebrow whitespace-nowrap">
                    Value
                  </th>
                  <th className="px-4 py-2.5 text-left eyebrow whitespace-nowrap">
                    Period
                  </th>
                  <th className="px-4 py-2.5 text-left eyebrow">Confidence</th>
                  <th className="px-4 py-2.5 text-left eyebrow">Version</th>
                  <th className="px-4 py-2.5 text-left eyebrow">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredChains.map((chain, i) => (
                  <ChainRow
                    key={chain.active.assumption_id}
                    chain={chain}
                    index={i}
                    isExpanded={expandedId === chain.active.assumption_id}
                    openPanel={
                      expandedId === chain.active.assumption_id
                        ? openPanel
                        : null
                    }
                    onTogglePanel={(panel) =>
                      handleTogglePanel(chain.active.assumption_id, panel)
                    }
                    onSupersede={() => handleOpenSupersede(chain.active)}
                    geographies={geographies}
                    subcategories={subcategories}
                    companies={companies}
                    canEdit={canEdit}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ---- Add / supersede modal ---- */}
      {modalOpen && (
        <AddAssumptionModal
          geographies={geographies}
          subcategories={subcategories}
          companies={companies}
          sources={sources}
          supersedingAssumption={supersedingAssumption}
          onClose={handleModalClose}
          onSuccess={handleModalSuccess}
        />
      )}
    </>
  );
}
