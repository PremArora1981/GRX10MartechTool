"use client";

/**
 * New Brief — NL-Brief Opener (W3 demo hook).
 *
 * A client component (no SSR data fetch needed — the whole point is user-driven
 * interpretation). The root layout already wraps this in NavShell.
 *
 * Flow:
 *   1. User edits the pre-filled example brief in the textarea.
 *   2. "Interpret" POSTs to /brief/interpret via api.interpretBrief.
 *   3. An editable result card appears with family chips, geography chips,
 *      year-range inputs, constraint chips, and recommended sources.
 *   4. "View the market model" navigates to /cells (the Cell Explorer).
 */

import { useState, useCallback } from "react";
import type { ReactNode } from "react";
import { useRouter } from "next/navigation";
import { PageHeader } from "@/components";
import { api, ApiError } from "@/lib/api";
import type {
  BriefConnectorPlanItem,
  BriefExecutionStep,
  BriefInterpretation,
  BriefMethodPlanItem,
  BriefProposedSubcategory,
  BriefRecommendedSource,
  EngagementCreateResult,
} from "@/lib/types";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const EXAMPLE_BRIEF =
  "Make a medtech market report for Southeast Asia, 2024-2029, focused on cardiovascular devices, exclude China";

// ---------------------------------------------------------------------------
// Small sub-components
// ---------------------------------------------------------------------------

function Chip({
  label,
  colorClass,
  onRemove,
}: {
  label: string;
  colorClass: string;
  onRemove: () => void;
}) {
  return (
    <span className={`badge ${colorClass} gap-1.5`}>
      {label}
      <button
        type="button"
        onClick={onRemove}
        className="ml-0.5 opacity-60 hover:opacity-100 transition-opacity leading-none"
        aria-label={`Remove ${label}`}
      >
        ×
      </button>
    </span>
  );
}

function SectionCard({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="card p-5">
      <h2 className="mb-3 text-sm font-semibold text-ink">{title}</h2>
      {children}
    </div>
  );
}

const CLASS_CHIP: Record<string, string> = {
  A: "bg-emerald-50 text-emerald-700",
  B: "bg-sky-50 text-sky-700",
  C: "bg-amber-50 text-amber-700",
};

const TIER_CHIP: Record<string, string> = {
  A: "bg-emerald-50 text-emerald-700",
  B: "bg-sky-50 text-sky-700",
  C: "bg-amber-50 text-amber-700",
};

function statusChipClass(status: string): string {
  if (status.startsWith("connected")) return "bg-emerald-50 text-emerald-700";
  if (status.includes("credential")) return "bg-amber-50 text-amber-700";
  return "bg-surface-subtle text-ink-muted";
}

/** Numbered phased steps: how the platform turns the brief into a model. */
function ExecutionPlanSection({ steps }: { steps: BriefExecutionStep[] }) {
  return (
    <SectionCard title="Execution Plan — what happens next">
      <ol className="space-y-0">
        {steps.map((s, i) => (
          <li key={s.step} className="relative flex gap-4 pb-5 last:pb-0">
            {/* connector line */}
            {i < steps.length - 1 && (
              <span
                className="absolute left-[13px] top-7 h-full w-px bg-line"
                aria-hidden
              />
            )}
            <span className="relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand/10 text-xs font-semibold text-brand">
              {s.step}
            </span>
            <div className="min-w-0 pt-0.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-ink">{s.title}</span>
                <span className="badge bg-surface-subtle text-ink-muted">
                  {s.phase}
                </span>
                <span className="text-xs text-ink-subtle">{s.timeline}</span>
              </div>
              <p className="mt-1 text-xs leading-relaxed text-ink-muted">
                {s.detail}
              </p>
            </div>
          </li>
        ))}
      </ol>
    </SectionCard>
  );
}

/** Small × control shared by the editable plan lists. */
function RemoveButton({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <button
      type="button"
      onClick={onRemove}
      aria-label={`Remove ${label}`}
      title={`Remove ${label} from the plan`}
      className="ml-auto shrink-0 rounded p-1 text-ink-subtle opacity-60 transition-opacity hover:bg-surface-subtle hover:opacity-100"
    >
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden>
        <path d="M18 6L6 18M6 6l12 12" />
      </svg>
    </button>
  );
}

/** Connector plan: which sources get engaged, what they pull, how it's parsed. */
function ConnectorPlanSection({
  plan,
  onRemove,
}: {
  plan: BriefConnectorPlanItem[];
  onRemove: (sourceId: string) => void;
}) {
  return (
    <SectionCard
      title={`Connector Plan — ${plan.length} sources, ingestion & parsing`}
    >
      <p className="mb-3 text-xs text-ink-muted">
        Editable — remove any source you don&apos;t want in this engagement;
        more can be added later from the Connectors catalog.
      </p>
      <ul className="divide-y divide-line">
        {plan.map((c) => (
          <li key={c.source_id} className="py-3 first:pt-0 last:pb-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`badge shrink-0 ${CLASS_CHIP[c.source_class] ?? CLASS_CHIP.B}`}>
                Class {c.source_class}
              </span>
              <span className="text-sm font-medium text-ink">{c.publisher}</span>
              <span className={`badge ${statusChipClass(c.status)}`}>{c.status}</span>
              <span className="text-xs text-ink-subtle">{c.access}</span>
              <span className="hidden font-mono text-[11px] text-ink-subtle sm:inline">
                → {c.raw_table}
              </span>
              <RemoveButton label={c.publisher} onRemove={() => onRemove(c.source_id)} />
            </div>
            <div className="mt-1.5 grid gap-1 text-xs leading-relaxed text-ink-muted sm:grid-cols-2 sm:gap-4">
              <p>
                <span className="font-medium text-ink-subtle">Pulls: </span>
                {c.pulls}
              </p>
              <p>
                <span className="font-medium text-ink-subtle">Parsing: </span>
                {c.parsing}
              </p>
            </div>
          </li>
        ))}
        {plan.length === 0 && (
          <li className="py-3 text-sm italic text-ink-muted">
            All connectors removed — add sources from the Connectors catalog
            before running ingestion.
          </li>
        )}
      </ul>
    </SectionCard>
  );
}

/** Estimation methodology: the methods that will triangulate each cell. */
function MethodPlanSection({
  plan,
  onRemove,
}: {
  plan: BriefMethodPlanItem[];
  onRemove: (methodCode: string) => void;
}) {
  const tierA = plan.filter((m) => m.tier === "A").length;
  return (
    <SectionCard
      title={`Estimation Methodology — ${plan.length} triangulation methods`}
    >
      <p className="mb-3 text-xs text-ink-muted">
        Editable — remove methods you don&apos;t want. Every market cell needs
        at least two independent methods before it enters the model
        {tierA < 2 && plan.length > 0 && (
          <span className="font-medium text-amber-700">
            {" "}(warning: fewer than 2 tier-A methods left — HIGH confidence
            becomes unreachable)
          </span>
        )}
        ; confidence is scored per cell from method count, spread, and
        source-class independence.
      </p>
      <ul className="divide-y divide-line">
        {plan.map((m) => (
          <li key={m.method_code} className="py-3 first:pt-0 last:pb-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`badge shrink-0 ${TIER_CHIP[m.tier] ?? TIER_CHIP.C}`}>
                Tier {m.tier}
              </span>
              <span className="font-mono text-sm font-medium text-ink">
                {m.method_code}
              </span>
              {m.feeds_from.length > 0 && (
                <span className="text-[11px] text-ink-subtle">
                  reads {m.feeds_from.join(", ")}
                </span>
              )}
              <RemoveButton label={m.method_code} onRemove={() => onRemove(m.method_code)} />
            </div>
            <p className="mt-1 text-xs leading-relaxed text-ink-muted">
              {m.methodology || m.description}
            </p>
          </li>
        ))}
        {plan.length === 0 && (
          <li className="py-3 text-sm italic text-ink-muted">
            All methods removed — restore by re-interpreting the brief.
          </li>
        )}
      </ul>
    </SectionCard>
  );
}

/** Proposed subcategories for a new-vertical brief, grouped by family. */
function ProposedSubcategoriesSection({
  subcategories,
}: {
  subcategories: BriefProposedSubcategory[];
}) {
  // Group by family, preserving first-seen order.
  const byFamily: { family: string; items: BriefProposedSubcategory[] }[] = [];
  for (const sub of subcategories) {
    let group = byFamily.find((g) => g.family === sub.family);
    if (!group) {
      group = { family: sub.family, items: [] };
      byFamily.push(group);
    }
    group.items.push(sub);
  }

  return (
    <SectionCard
      title={`Proposed Subcategories — ${subcategories.length} for a new vertical`}
    >
      <p className="mb-3 text-xs text-ink-muted">
        This brief falls outside the current taxonomy — the platform proposes the
        subcategories below (with candidate HS &amp; regulatory codes) to seed the
        new vertical.
      </p>
      <div className="space-y-4">
        {byFamily.map((group) => (
          <div key={group.family}>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-subtle">
              {group.family}
            </h3>
            <ul className="divide-y divide-line">
              {group.items.map((sub) => (
                <li key={`${group.family}::${sub.name}`} className="py-2.5 first:pt-0 last:pb-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-ink">{sub.name}</span>
                    {sub.hs_codes.map((code) => (
                      <span key={`hs-${code}`} className="badge bg-sky-50 font-mono text-sky-700">
                        HS {code}
                      </span>
                    ))}
                    {sub.regulatory_codes.map((code) => (
                      <span key={`reg-${code}`} className="badge bg-amber-50 font-mono text-amber-700">
                        {code}
                      </span>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </SectionCard>
  );
}

// ---------------------------------------------------------------------------
// Main page component
// ---------------------------------------------------------------------------

export default function BriefPage() {
  const router = useRouter();

  // Input state
  const [briefText, setBriefText] = useState(EXAMPLE_BRIEF);

  // Request state
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Result + editable copies
  const [result, setResult] = useState<BriefInterpretation | null>(null);
  const [families, setFamilies] = useState<string[]>([]);
  const [geographies, setGeographies] = useState<string[]>([]);
  const [yearFrom, setYearFrom] = useState(2026);
  const [yearTo, setYearTo] = useState(2031);
  const [constraints, setConstraints] = useState<string[]>([]);
  const [connectorPlan, setConnectorPlan] = useState<BriefConnectorPlanItem[]>([]);
  const [methodPlan, setMethodPlan] = useState<BriefMethodPlanItem[]>([]);

  // Create-engagement flow state
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [engagementName, setEngagementName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createResult, setCreateResult] = useState<EngagementCreateResult | null>(null);
  const [populating, setPopulating] = useState(false);

  // ── Handlers ──────────────────────────────────────────────────────────────

  const handleInterpret = useCallback(async () => {
    const text = briefText.trim();
    if (!text) return;
    setLoading(true);
    setError(null);
    try {
      const data = await api.interpretBrief(text);
      setResult(data);
      setFamilies(data.families);
      setGeographies(data.geographies);
      setYearFrom(data.years.from);
      setYearTo(data.years.to);
      setConstraints(data.constraints);
      setConnectorPlan(data.connector_plan ?? []);
      setMethodPlan(data.method_plan ?? []);
      // Reset the create-engagement flow for the fresh interpretation.
      setShowCreateForm(false);
      setCreateResult(null);
      setCreateError(null);
      // Prefill an engagement name from the interpreted scope.
      const famPart = data.families.length > 0 ? data.families.join(", ") : "All families";
      const geoPart = data.geographies.length > 0 ? ` — ${data.geographies.join(", ")}` : "";
      setEngagementName(`${famPart}${geoPart} (${data.years.from}–${data.years.to})`);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Interpretation failed — please try again.";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [briefText]);

  const removeFamily = useCallback(
    (f: string) => setFamilies((prev) => prev.filter((x) => x !== f)),
    [],
  );

  const removeGeo = useCallback(
    (g: string) => setGeographies((prev) => prev.filter((x) => x !== g)),
    [],
  );

  const removeConstraint = useCallback(
    (c: string) => setConstraints((prev) => prev.filter((x) => x !== c)),
    [],
  );

  const removeConnector = useCallback(
    (sourceId: string) =>
      setConnectorPlan((prev) => prev.filter((c) => c.source_id !== sourceId)),
    [],
  );

  const removeMethod = useCallback(
    (methodCode: string) =>
      setMethodPlan((prev) => prev.filter((m) => m.method_code !== methodCode)),
    [],
  );

  const handleCreate = useCallback(async () => {
    if (!result) return;
    const name = engagementName.trim();
    if (!name) {
      setCreateError("Please give the engagement a name.");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const res = await api.createEngagement({
        name,
        brief_text: briefText,
        families,
        geographies,
        year_from: yearFrom,
        year_to: yearTo,
        plan: {
          families,
          geographies,
          proposed_subcategories: result.proposed_subcategories ?? [],
          connector_plan: connectorPlan,
          method_plan: methodPlan,
          taxonomy_status: result.taxonomy_status,
          web_search_enabled: true,
        },
      });
      // Backend is a different origin — its Set-Cookie won't stick, so set it
      // client-side to scope subsequent requests to the new engagement.
      document.cookie = `engagement_id=${res.engagement_id}; path=/; max-age=31536000; samesite=lax`;
      setCreateResult(res);
      setShowCreateForm(false);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to create the engagement — please try again.";
      setCreateError(msg);
    } finally {
      setCreating(false);
    }
  }, [
    result,
    engagementName,
    briefText,
    families,
    geographies,
    yearFrom,
    yearTo,
    connectorPlan,
    methodPlan,
  ]);

  const handlePopulate = useCallback(async () => {
    if (!createResult) return;
    setPopulating(true);
    setCreateError(null);
    try {
      await api.populateEngagement(createResult.engagement_id);
      window.location.href = "/";
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to start population — please try again.";
      setCreateError(msg);
      setPopulating(false);
    }
  }, [createResult]);

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div>
      <PageHeader
        eyebrow="Demo Hook · W3"
        title="New Research Brief"
        description="Describe your market research scope in plain language. The platform interprets it into a structured plan you can edit before exploring the model."
      />

      {/* ── Brief input card ── */}
      <div className="card mb-6 p-5">
        <label
          htmlFor="brief-textarea"
          className="mb-2 block text-sm font-medium text-ink"
        >
          Your brief
        </label>
        <textarea
          id="brief-textarea"
          rows={4}
          value={briefText}
          onChange={(e) => setBriefText(e.target.value)}
          placeholder="e.g. Make a medtech market report for Southeast Asia, 2024-2029, focused on cardiovascular devices…"
          className="w-full resize-none rounded-lg border border-line bg-surface-subtle px-3 py-2.5 text-sm text-ink placeholder-ink-subtle focus:outline-none focus:ring-2 focus:ring-brand"
        />
        <div className="mt-3 flex items-center gap-3">
          <button
            type="button"
            onClick={handleInterpret}
            disabled={loading || !briefText.trim()}
            className="inline-flex items-center gap-2 rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-brand/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? (
              <>
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white border-t-transparent" />
                Interpreting…
              </>
            ) : (
              <>
                <svg
                  viewBox="0 0 24 24"
                  className="h-4 w-4"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.7}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden
                >
                  <path d="M9 12h6M12 9v6M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                Interpret
              </>
            )}
          </button>
          {error && (
            <p className="text-sm text-red-600" role="alert">
              {error}
            </p>
          )}
        </div>
      </div>

      {/* ── Editable result ── */}
      {result && (
        <div className="space-y-5">
          {/* Taxonomy status banner */}
          {result.taxonomy_status && !result.taxonomy_status.in_catalog && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
              <div className="text-sm font-semibold text-amber-800">
                New engagement — outside the current taxonomy
              </div>
              <p className="mt-1 text-xs leading-relaxed text-amber-700">
                {result.taxonomy_status.note}
              </p>
              {result.taxonomy_status.proposed_families.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {result.taxonomy_status.proposed_families.map((f) => (
                    <span key={f} className="badge bg-amber-100 text-amber-800">
                      {f} (proposed)
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Proposed subcategories (new-vertical briefs only) */}
          {(result.proposed_subcategories?.length ?? 0) > 0 && (
            <ProposedSubcategoriesSection
              subcategories={result.proposed_subcategories!}
            />
          )}

          {/* Families */}
          <SectionCard title="Product Families">
            <div className="flex flex-wrap gap-2">
              {families.length === 0 ? (
                <span className="text-sm italic text-ink-muted">
                  No families selected — all families in scope by default.
                </span>
              ) : (
                families.map((f) => (
                  <Chip
                    key={f}
                    label={f}
                    colorClass="bg-brand/10 text-brand"
                    onRemove={() => removeFamily(f)}
                  />
                ))
              )}
            </div>
          </SectionCard>

          {/* Geographies + Year range */}
          <div className="grid gap-5 sm:grid-cols-2">
            <SectionCard title="Geographies">
              <div className="flex flex-wrap gap-2">
                {geographies.length === 0 ? (
                  <span className="text-sm italic text-ink-muted">
                    No geographies selected.
                  </span>
                ) : (
                  geographies.map((g) => (
                    <Chip
                      key={g}
                      label={g}
                      colorClass="bg-sky-50 text-sky-700"
                      onRemove={() => removeGeo(g)}
                    />
                  ))
                )}
              </div>
            </SectionCard>

            <SectionCard title="Year Range">
              <div className="flex items-center gap-3">
                <input
                  type="number"
                  value={yearFrom}
                  onChange={(e) => setYearFrom(Number(e.target.value))}
                  min={2020}
                  max={2040}
                  className="w-24 rounded border border-line px-2 py-1.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-brand"
                  aria-label="Year from"
                />
                <span className="text-sm text-ink-muted">to</span>
                <input
                  type="number"
                  value={yearTo}
                  onChange={(e) => setYearTo(Number(e.target.value))}
                  min={2020}
                  max={2040}
                  className="w-24 rounded border border-line px-2 py-1.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-brand"
                  aria-label="Year to"
                />
              </div>
            </SectionCard>
          </div>

          {/* Constraints (only shown when present) */}
          {constraints.length > 0 && (
            <SectionCard title="Constraints">
              <div className="flex flex-wrap gap-2">
                {constraints.map((c) => (
                  <Chip
                    key={c}
                    label={c}
                    colorClass="bg-amber-50 text-amber-700"
                    onRemove={() => removeConstraint(c)}
                  />
                ))}
              </div>
            </SectionCard>
          )}

          {/* Execution blueprint: next steps, connectors, methodology */}
          {(result.execution_plan?.length ?? 0) > 0 && (
            <ExecutionPlanSection steps={result.execution_plan!} />
          )}
          {(result.connector_plan?.length ?? 0) > 0 && (
            <ConnectorPlanSection plan={connectorPlan} onRemove={removeConnector} />
          )}
          {(result.method_plan?.length ?? 0) > 0 && (
            <MethodPlanSection plan={methodPlan} onRemove={removeMethod} />
          )}

          {/* Recommended sources (legacy view — superseded by the connector plan) */}
          {(result.connector_plan?.length ?? 0) === 0 && result.recommended_sources.length > 0 && (
            <SectionCard title="Recommended Sources (and why)">
              <ul className="divide-y divide-line">
                {result.recommended_sources.map((src: BriefRecommendedSource) => (
                  <li
                    key={src.source_id}
                    className="flex items-start gap-3 py-3 first:pt-0 last:pb-0"
                  >
                    <span
                      className={`badge mt-0.5 shrink-0 ${
                        src.source_class === "A"
                          ? "bg-emerald-50 text-emerald-700"
                          : "bg-sky-50 text-sky-700"
                      }`}
                    >
                      Class {src.source_class}
                    </span>
                    <div className="min-w-0">
                      <div className="text-sm font-medium text-ink">
                        {src.publisher}
                      </div>
                      <div className="mt-0.5 text-xs leading-relaxed text-ink-muted">
                        {src.why}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            </SectionCard>
          )}

          {/* Interpretation notes */}
          {result.interpretation_notes && (
            <div className="rounded-lg border border-line bg-surface-subtle px-4 py-3 text-sm text-ink-muted">
              <span className="font-medium text-ink-subtle">Note: </span>
              {result.interpretation_notes}
            </div>
          )}

          {/* CTA + create-engagement flow */}
          <div className="pt-2">
            {createResult ? (
              /* ── Cost banner (post-create; does NOT auto-run) ── */
              <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-4">
                <div className="text-sm font-semibold text-emerald-800">
                  Created {createResult.name}: {createResult.planned_cells} planned
                  cells across {createResult.subcategories} subcategories ×{" "}
                  {createResult.geographies} geographies.
                </div>
                <p className="mt-1 text-xs leading-relaxed text-emerald-700">
                  Populate now runs ~{createResult.planned_cells} web searches (a
                  few minutes).
                  {createResult.capped &&
                    " Note: the grid was capped to stay within limits."}
                </p>
                {createError && (
                  <p className="mt-2 text-sm text-red-600" role="alert">
                    {createError}
                  </p>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    onClick={handlePopulate}
                    disabled={populating}
                    className="inline-flex items-center gap-2 rounded-lg bg-brand px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-brand/90 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {populating ? (
                      <>
                        <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white border-t-transparent" />
                        Populating…
                      </>
                    ) : (
                      "Populate now"
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      window.location.href = "/";
                    }}
                    disabled={populating}
                    className="inline-flex items-center gap-2 rounded-lg border border-line bg-surface px-5 py-2.5 text-sm font-medium text-ink transition-colors hover:bg-surface-subtle disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Skip — explore empty
                  </button>
                </div>
              </div>
            ) : showCreateForm ? (
              /* ── Inline create form ── */
              <div className="card p-5">
                <label
                  htmlFor="engagement-name"
                  className="mb-2 block text-sm font-medium text-ink"
                >
                  Engagement name
                </label>
                <input
                  id="engagement-name"
                  type="text"
                  value={engagementName}
                  onChange={(e) => setEngagementName(e.target.value)}
                  placeholder="e.g. Cardiovascular devices — Southeast Asia (2024–2029)"
                  className="w-full rounded-lg border border-line bg-surface-subtle px-3 py-2.5 text-sm text-ink placeholder-ink-subtle focus:outline-none focus:ring-2 focus:ring-brand"
                />
                {createError && (
                  <p className="mt-2 text-sm text-red-600" role="alert">
                    {createError}
                  </p>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    onClick={handleCreate}
                    disabled={creating || !engagementName.trim()}
                    className="inline-flex items-center gap-2 rounded-lg bg-brand px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-brand/90 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {creating ? (
                      <>
                        <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white border-t-transparent" />
                        Creating…
                      </>
                    ) : (
                      "Create engagement"
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setShowCreateForm(false);
                      setCreateError(null);
                    }}
                    disabled={creating}
                    className="text-sm font-medium text-ink-muted transition-colors hover:text-ink disabled:opacity-50"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              /* ── Primary CTAs ── */
              <div className="flex flex-wrap justify-end gap-3">
                <button
                  type="button"
                  onClick={() => router.push("/cells")}
                  className="inline-flex items-center gap-2 rounded-lg border border-line bg-surface px-5 py-2.5 text-sm font-medium text-ink transition-colors hover:bg-surface-subtle"
                >
                  View the market model
                  <svg
                    viewBox="0 0 24 24"
                    className="h-4 w-4"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden
                  >
                    <path d="M5 12h14M12 5l7 7-7 7" />
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setCreateError(null);
                    setShowCreateForm(true);
                  }}
                  className="inline-flex items-center gap-2 rounded-lg bg-brand px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-brand/90"
                >
                  Create engagement
                  <svg
                    viewBox="0 0 24 24"
                    className="h-4 w-4"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden
                  >
                    <path d="M5 12h14M12 5l7 7-7 7" />
                  </svg>
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
