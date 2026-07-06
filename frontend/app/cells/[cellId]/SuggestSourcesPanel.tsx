"use client";

/**
 * SuggestSourcesPanel — AI-assisted source discovery for a market-sizing cell.
 *
 * Most valuable for Unsized / LOW-confidence cells that have few or no
 * triangulation estimates. It calls an LLM-backed backend endpoint that:
 *   1. Diagnoses *why* the cell is empty or thin (the `diagnosis` string).
 *   2. Proposes concrete data sources (publisher, class, base URL, auth) that
 *      could fill the gap, each with a `why` rationale.
 *
 * From here an analyst can add a suggestion straight to the connector registry
 * (POST add-suggested-source). The backend returns a `detail` string telling
 * them to drop an API key on the Connectors page and Pull.
 *
 * This is a client component: the suggest call is a ~20–40s LLM request that
 * needs local loading / error / "added" state, and the add action mutates.
 */

import { useState } from "react";
import { api, ApiError } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types — inferred from the API client so this stays in sync with lib/api.ts.
// ---------------------------------------------------------------------------

type SuggestResult = Awaited<ReturnType<typeof api.suggestCellSources>>;
type Suggestion = SuggestResult["suggestions"][number];

type AddState =
  | { status: "idle" }
  | { status: "adding" }
  | { status: "added"; detail: string }
  | { status: "error"; message: string };

// ---------------------------------------------------------------------------
// Source-class badge — A=emerald/Primary, B=sky/Secondary, C=amber/Tertiary.
// ---------------------------------------------------------------------------

const CLASS_BADGE: Record<
  string,
  { cls: string; label: string; title: string }
> = {
  A: {
    cls: "bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-600/20",
    label: "Primary",
    title: "Class A — Primary structured source (qualifies HIGH confidence)",
  },
  B: {
    cls: "bg-sky-50 text-sky-700 ring-1 ring-inset ring-sky-600/20",
    label: "Secondary",
    title: "Class B — Industry / procedural source (qualifies MEDIUM confidence)",
  },
  C: {
    cls: "bg-amber-50 text-amber-700 ring-1 ring-inset ring-amber-600/20",
    label: "Tertiary",
    title: "Class C — Triangulation support (gap-fill / scaling only)",
  },
};

function ClassBadge({ cls }: { cls: string }) {
  const meta = CLASS_BADGE[cls] ?? {
    cls: "bg-surface-subtle text-ink-muted",
    label: cls,
    title: `Source class ${cls}`,
  };
  return (
    <span
      className={`badge text-2xs font-semibold ${meta.cls}`}
      title={meta.title}
      aria-label={meta.title}
    >
      {cls} · {meta.label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Single suggestion card
// ---------------------------------------------------------------------------

function SuggestionCard({
  suggestion,
  state,
  onAdd,
}: {
  suggestion: Suggestion;
  state: AddState;
  onAdd: () => void;
}) {
  const added = state.status === "added";

  return (
    <li className="rounded-lg border border-line bg-white p-4 shadow-card">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <ClassBadge cls={suggestion.source_class} />
            <span className="text-sm font-semibold text-ink">
              {suggestion.publisher}
            </span>
          </div>
          <p className="mt-1.5 text-sm text-ink-muted">{suggestion.why}</p>
          <p className="mt-1.5 break-all font-mono text-xs text-ink-subtle">
            {suggestion.base_url}
            {suggestion.endpoint_path}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-2xs text-ink-muted">
            <span className="badge bg-surface-subtle text-ink-muted">
              {suggestion.auth_type}
            </span>
            <span className="badge bg-surface-subtle text-ink-muted">
              {suggestion.raw_table}
            </span>
          </div>
        </div>

        {/* Add-to-connectors action */}
        <div className="shrink-0">
          {added ? (
            <span
              className="badge bg-emerald-50 text-2xs font-semibold text-emerald-700 ring-1 ring-inset ring-emerald-600/20"
              aria-label="Added to connectors"
            >
              Added ✓
            </span>
          ) : (
            <button
              type="button"
              onClick={onAdd}
              disabled={state.status === "adding"}
              className="btn-secondary whitespace-nowrap text-xs disabled:cursor-not-allowed disabled:opacity-60"
            >
              {state.status === "adding" ? "Adding…" : "Add to connectors"}
            </button>
          )}
        </div>
      </div>

      {/* Success detail — tells the analyst to add a key + Pull */}
      {added && (
        <p className="mt-3 rounded-md bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          {state.detail}
        </p>
      )}

      {/* Inline add error */}
      {state.status === "error" && (
        <p className="mt-3 rounded-md bg-confidence-low-bg px-3 py-2 text-xs text-confidence-low">
          {state.message}
        </p>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// SuggestSourcesPanel (exported)
// ---------------------------------------------------------------------------

export interface SuggestSourcesPanelProps {
  cellId: number;
  /** When false, the cell has no triangulation estimates — lead with this panel. */
  hasEstimates: boolean;
}

export function SuggestSourcesPanel({
  cellId,
  hasEstimates,
}: SuggestSourcesPanelProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SuggestResult | null>(null);
  const [addStates, setAddStates] = useState<Record<number, AddState>>({});

  async function runSuggest() {
    setLoading(true);
    setError(null);
    try {
      const data = await api.suggestCellSources(cellId);
      setResult(data);
      setAddStates({});
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not fetch source suggestions. Please try again.",
      );
    } finally {
      setLoading(false);
    }
  }

  async function addSource(index: number, suggestion: Suggestion) {
    setAddStates((prev) => ({ ...prev, [index]: { status: "adding" } }));
    try {
      const res = await api.addSuggestedSource(cellId, {
        publisher: suggestion.publisher,
        source_class: suggestion.source_class,
        raw_table: suggestion.raw_table,
        base_url: suggestion.base_url,
        endpoint_path: suggestion.endpoint_path,
        auth_type: suggestion.auth_type,
      });
      setAddStates((prev) => ({
        ...prev,
        [index]: { status: "added", detail: res.detail },
      }));
    } catch (err) {
      setAddStates((prev) => ({
        ...prev,
        [index]: {
          status: "error",
          message:
            err instanceof ApiError
              ? err.message
              : "Could not add this source. Please try again.",
        },
      }));
    }
  }

  const cta = result ? "Suggest more sources" : "Suggest data sources";

  return (
    <section aria-labelledby="suggest-sources-heading">
      {/* Section header + trigger */}
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2
            id="suggest-sources-heading"
            className="text-base font-semibold text-ink"
          >
            AI source suggestions
          </h2>
          <p className="mt-0.5 text-xs text-ink-muted">
            {hasEstimates
              ? "Find additional data sources to strengthen this cell's triangulation."
              : "This cell has no estimates yet — let AI diagnose why and propose sources to fill the gap."}
          </p>
        </div>

        <button
          type="button"
          onClick={runSuggest}
          disabled={loading}
          className={`${
            hasEstimates ? "btn-secondary" : "btn-primary"
          } whitespace-nowrap text-sm disabled:cursor-not-allowed disabled:opacity-60`}
        >
          {loading ? "Finding sources…" : cta}
        </button>
      </div>

      {/* Loading state — this is a 20–40s LLM call */}
      {loading && (
        <div
          className="card flex items-center justify-center gap-3 py-14 text-center"
          role="status"
          aria-live="polite"
        >
          <span
            className="h-5 w-5 animate-spin rounded-full border-2 border-brand-200 border-t-brand"
            aria-hidden
          />
          <span className="text-sm text-ink-muted">
            Finding sources… this can take 20–40 seconds.
          </span>
        </div>
      )}

      {/* Fetch error */}
      {!loading && error && (
        <div className="card p-5">
          <p className="text-sm font-medium text-confidence-low">{error}</p>
          <button
            type="button"
            onClick={runSuggest}
            className="btn-secondary mt-3 text-xs"
          >
            Try again
          </button>
        </div>
      )}

      {/* Results */}
      {!loading && result && (
        <div className="space-y-4">
          {/* Diagnosis — prominent */}
          <div className="card border-l-4 border-l-brand p-5">
            <div className="eyebrow mb-1.5">Diagnosis</div>
            <p className="text-sm text-ink">{result.diagnosis}</p>

            {result.existing_sources.length > 0 && (
              <div className="mt-3 border-t border-line pt-3">
                <div className="eyebrow mb-1.5">
                  Existing sources ({result.existing_sources.length})
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {result.existing_sources.map((s) => (
                    <span
                      key={s}
                      className="badge bg-surface-subtle text-2xs text-ink-muted"
                    >
                      {s}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Suggestions list */}
          {result.suggestions.length === 0 ? (
            <div className="card py-10 text-center">
              <p className="text-sm text-ink-muted">
                No new sources were suggested for this cell.
              </p>
            </div>
          ) : (
            <div>
              <h3 className="mb-2 text-sm font-semibold text-ink">
                Suggested sources ({result.suggestions.length})
              </h3>
              <ul className="space-y-3">
                {result.suggestions.map((suggestion, i) => (
                  <SuggestionCard
                    key={`${suggestion.publisher}-${suggestion.endpoint_path}-${i}`}
                    suggestion={suggestion}
                    state={addStates[i] ?? { status: "idle" }}
                    onAdd={() => addSource(i, suggestion)}
                  />
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
