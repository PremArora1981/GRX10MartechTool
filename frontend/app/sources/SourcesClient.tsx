"use client";

/**
 * SourcesClient — interactive Sources View (W2).
 *
 * Renders the full source registry grouped by class (A → B → C), with:
 *  - ConnectorHealthBadge (last_probe_status)
 *  - "Why it matters" rationale line
 *  - Method chips ("used by: <method_codes>")
 *  - A "Recommended sources by method" section (method → source chips)
 *
 * All data is pre-fetched on the server (page.tsx) and passed as props.
 * No SWR here — source metadata changes rarely; the server render is fresh
 * because api.ts defaults to cache: 'no-store'.
 */

import { useState } from "react";
import { ConnectorHealthBadge } from "@/components";
import { api } from "@/lib/api";
import type { RecommendedSources, SourceClass, SourceDetail } from "@/lib/types";

// ---------------------------------------------------------------------------
// Local helpers
// ---------------------------------------------------------------------------

const CLASS_LABEL: Record<string, { heading: string; pill: string; border: string }> = {
  A: {
    heading: "Class A — Primary structured evidence",
    pill: "bg-emerald-100 text-emerald-800",
    border: "border-emerald-200",
  },
  B: {
    heading: "Class B — Industry / procedural cross-check",
    pill: "bg-sky-100 text-sky-800",
    border: "border-sky-200",
  },
  C: {
    heading: "Class C — Triangulation support (gap-fill / scaling)",
    pill: "bg-violet-100 text-violet-800",
    border: "border-violet-200",
  },
};

const ACCESS_LABEL: Record<string, string> = {
  api: "API",
  scrape: "Scrape",
  web_search: "Web search",
  manual_upload: "Manual upload",
};

function ClassPill({ cls }: { cls: SourceClass | null | undefined }) {
  if (!cls) return null;
  const style = CLASS_LABEL[cls];
  if (!style) return null;
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-semibold ${style.pill}`}>
      {cls}
    </span>
  );
}

function MethodChip({ code }: { code: string }) {
  return (
    <span className="inline-block rounded bg-surface-subtle px-1.5 py-0.5 font-mono text-2xs text-ink-subtle">
      {code}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Source card
// ---------------------------------------------------------------------------

interface SourceCardProps {
  source: SourceDetail;
  classBorder: string;
}

function SourceCard({ source, classBorder }: SourceCardProps) {
  // Enable/disable is optimistic-local: the server render passed the initial
  // value; the toggle round-trips POST /connectors/{id}/enable|disable.
  const [enabled, setEnabled] = useState(source.enabled !== false);
  const [toggling, setToggling] = useState(false);

  async function handleToggle() {
    const next = !enabled;
    setToggling(true);
    setEnabled(next); // optimistic
    try {
      if (next) await api.enableSource(source.source_id);
      else await api.disableSource(source.source_id);
    } catch {
      setEnabled(!next); // revert on failure
    } finally {
      setToggling(false);
    }
  }

  return (
    <div
      className={`rounded-lg border ${classBorder} bg-surface p-4 shadow-sm ${enabled ? "" : "opacity-60"}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <ClassPill cls={source.source_class as SourceClass | null} />
            <span className="text-sm font-semibold text-ink">{source.publisher}</span>
            {source.access_method && (
              <span className="rounded bg-surface-subtle px-1.5 py-0.5 text-2xs text-ink-muted">
                {ACCESS_LABEL[source.access_method] ?? source.access_method}
              </span>
            )}
            {source.raw_table && (
              <span className="font-mono text-2xs text-ink-subtle">
                → {source.raw_table}
              </span>
            )}
          </div>

          {/* Why it matters */}
          <p className="mt-1.5 text-xs text-ink-subtle leading-relaxed">{source.why}</p>

          {/* Method usage chips */}
          {source.used_for.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1 items-center">
              <span className="text-2xs text-ink-muted mr-1">Used by:</span>
              {source.used_for.map((code) => (
                <MethodChip key={code} code={code} />
              ))}
            </div>
          )}
        </div>

        {/* Health badge + enable toggle — right-aligned */}
        <div className="flex shrink-0 items-center gap-2 pt-0.5">
          <ConnectorHealthBadge
            status={source.last_probe_status}
            detail={source.last_probe_detail ?? undefined}
            size="sm"
          />
          <button
            type="button"
            role="switch"
            aria-checked={enabled}
            disabled={toggling}
            onClick={handleToggle}
            title={enabled ? "Disable — pipeline stops pulling this source" : "Enable — pipeline resumes pulling this source"}
            className={`focusable relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors disabled:opacity-50 ${
              enabled ? "bg-emerald-500" : "bg-slate-300"
            }`}
          >
            <span
              className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform ${
                enabled ? "translate-x-[18px]" : "translate-x-[3px]"
              }`}
              aria-hidden
            />
          </button>
          <span className="w-12 text-2xs text-ink-muted">
            {toggling ? "…" : enabled ? "enabled" : "disabled"}
          </span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Recommended sources section
// ---------------------------------------------------------------------------

interface RecommendedSectionProps {
  recommended: RecommendedSources;
  sourcesById: Map<string, SourceDetail>;
}

function RecommendedSection({ recommended, sourcesById }: RecommendedSectionProps) {
  const entries = Object.entries(recommended.method_map).filter(
    ([, ids]) => ids.length > 0,
  );

  if (entries.length === 0) {
    return (
      <p className="text-sm text-ink-subtle">
        No method-to-source mappings found. Enable sources and run a pipeline to
        populate the method registry.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {entries.map(([method, sourceIds]) => (
        <div key={method} className="rounded-lg border border-line bg-surface p-3">
          <div className="mb-2 flex items-center gap-2">
            <span className="font-mono text-xs font-semibold text-ink">{method}</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {sourceIds.map((sid) => {
              const src = sourcesById.get(sid);
              return (
                <span
                  key={sid}
                  className="flex items-center gap-1 rounded-full border border-line bg-surface-subtle px-2 py-0.5 text-xs"
                  title={src?.why}
                >
                  {src && <ClassPill cls={src.source_class as SourceClass | null} />}
                  <span className="text-ink">{src?.publisher ?? sid}</span>
                </span>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main client component
// ---------------------------------------------------------------------------

export interface SourcesClientProps {
  sources: SourceDetail[];
  recommended: RecommendedSources;
}

export function SourcesClient({ sources, recommended }: SourcesClientProps) {
  // Group by class, preserving server-side order (A → B → C → null).
  const grouped = new Map<string, SourceDetail[]>();
  for (const src of sources) {
    const cls = src.source_class ?? "?";
    if (!grouped.has(cls)) grouped.set(cls, []);
    grouped.get(cls)!.push(src);
  }

  // Index by source_id for fast lookup in the recommended section.
  const sourcesById = new Map<string, SourceDetail>(
    sources.map((s) => [s.source_id, s]),
  );

  const classOrder = ["A", "B", "C", "?"];

  return (
    <div className="space-y-10">
      {/* ---- Grouped source cards ---------------------------------------- */}
      {classOrder.map((cls) => {
        const rows = grouped.get(cls);
        if (!rows || rows.length === 0) return null;
        const meta = CLASS_LABEL[cls];
        return (
          <section key={cls} aria-labelledby={`class-${cls}-heading`}>
            <div className="mb-3 flex items-center gap-2">
              {meta && (
                <>
                  <span
                    className={`inline-block rounded-full px-2.5 py-1 text-sm font-bold ${meta.pill}`}
                  >
                    {cls}
                  </span>
                  <h2
                    id={`class-${cls}-heading`}
                    className="text-sm font-semibold text-ink"
                  >
                    {meta.heading}
                  </h2>
                </>
              )}
              <span className="text-xs text-ink-subtle">({rows.length} sources)</span>
            </div>

            <div className="grid gap-3 sm:grid-cols-1 lg:grid-cols-2">
              {rows.map((src) => (
                <SourceCard
                  key={src.source_id}
                  source={src}
                  classBorder={meta?.border ?? "border-line"}
                />
              ))}
            </div>
          </section>
        );
      })}

      {sources.length === 0 && (
        <div className="rounded-lg border border-line bg-surface p-8 text-center">
          <p className="text-sm font-medium text-ink">No sources registered yet.</p>
          <p className="mt-1 text-xs text-ink-subtle">
            Go to Connectors, select a source from the catalog, and it will appear here.
          </p>
        </div>
      )}

      {/* ---- Recommended sources by method ------------------------------- */}
      <section aria-labelledby="recommended-heading">
        <div className="mb-3">
          <h2 id="recommended-heading" className="text-base font-semibold text-ink">
            Recommended sources by method
          </h2>
          <p className="mt-0.5 text-xs text-ink-subtle">
            Which enabled sources feed each triangulation method, derived from the
            method registry&apos;s required raw-table declarations.
          </p>
        </div>
        <RecommendedSection recommended={recommended} sourcesById={sourcesById} />
      </section>
    </div>
  );
}

export default SourcesClient;
