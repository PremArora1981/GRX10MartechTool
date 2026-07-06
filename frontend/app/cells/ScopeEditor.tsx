"use client";

/**
 * ScopeEditor — collapsible panel to add/remove subcategories & geographies
 * on the current engagement AFTER it has been created.
 *
 * Rendered ABOVE the Cell Explorer table (from CellsClient). Adding or removing
 * a subcategory/geography changes the engagement's scope, which adds or removes
 * cells server-side — so on any successful mutation we hard-reload the page to
 * re-pull the cell list (and its filter option lists) from the server.
 *
 * Data sourcing:
 *   - The current subcategory + geography lists are the SAME lists the Cell
 *     Explorer already fetches for its filter dropdowns; CellsClient passes them
 *     straight through as props. We do NOT re-fetch them here.
 *   - The engagement id is resolved once on mount via api.currentEngagement(),
 *     falling back to the `engagement_id` cookie if that call fails.
 *
 * Styling reuses the app's Tailwind primitives: `card`, `eyebrow`, `border-line`,
 * `bg-surface`, brand button, etc. — matching the existing filter bar.
 */

import { useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "@/lib/api";
import { segmentLabel } from "@/lib/format";
import type { Geography, TaxonomySubcategory } from "@/lib/types";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ScopeEditorProps {
  /** Current subcategories — reused from the Cell Explorer filter dropdown. */
  subcategories: TaxonomySubcategory[];
  /** Current geographies — reused from the Cell Explorer filter dropdown. */
  geographies: Geography[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Read the `engagement_id` cookie as a fallback engagement source. */
function readEngagementCookie(): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie
    .split("; ")
    .find((c) => c.startsWith("engagement_id="));
  return match ? decodeURIComponent(match.slice("engagement_id=".length)) : null;
}

/** Split a comma/whitespace separated code list into a trimmed, de-duped array. */
function splitCodes(raw: string): string[] {
  const seen = new Set<string>();
  for (const part of raw.split(",")) {
    const t = part.trim();
    if (t) seen.add(t);
  }
  return [...seen];
}

// ---------------------------------------------------------------------------
// Small styled primitives (match the existing filter bar)
// ---------------------------------------------------------------------------

const INPUT_CLASS =
  "rounded-md border border-line bg-surface px-3 py-1.5 text-sm text-ink " +
  "shadow-sm transition-colors placeholder:text-ink-subtle focus:border-brand " +
  "focus:outline-none focus:ring-1 focus:ring-brand disabled:opacity-50";

function Field({
  id,
  label,
  children,
}: {
  id: string;
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="eyebrow">
        {label}
      </label>
      {children}
    </div>
  );
}

function PrimaryButton({
  children,
  disabled,
  type = "submit",
}: {
  children: ReactNode;
  disabled?: boolean;
  type?: "submit" | "button";
}) {
  return (
    <button
      type={type}
      disabled={disabled}
      className="focusable inline-flex items-center justify-center gap-2 rounded-lg
                 bg-brand px-4 py-2 text-sm font-medium text-white transition-colors
                 hover:bg-brand/90 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {children}
    </button>
  );
}

/** A short-lived status banner for the outcome of a mutation. */
function StatusBanner({
  status,
}: {
  status: { kind: "ok" | "err"; message: string } | null;
}) {
  if (!status) return null;
  const isOk = status.kind === "ok";
  return (
    <div
      role="status"
      aria-live="polite"
      className={
        "rounded-md border px-3 py-2 text-sm " +
        (isOk
          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700"
          : "border-red-500/40 bg-red-500/10 text-red-700")
      }
    >
      {status.message}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function ScopeEditor({
  subcategories,
  geographies,
}: ScopeEditorProps) {
  const [open, setOpen] = useState(false);
  const [engagementId, setEngagementId] = useState<string | null>(null);

  // Add-subcategory form state
  const [family, setFamily] = useState("");
  const [subName, setSubName] = useState("");
  const [hsCodes, setHsCodes] = useState("");

  // Add-geography form state
  const [country, setCountry] = useState("");

  // Shared UX state
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ kind: "ok" | "err"; message: string } | null>(
    null,
  );

  // Resolve the engagement id once on mount.
  useEffect(() => {
    let cancelled = false;
    api
      .currentEngagement()
      .then((e) => {
        if (!cancelled) setEngagementId(e.engagement_id);
      })
      .catch(() => {
        if (!cancelled) setEngagementId(readEngagementCookie());
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Only show non-superseded subcategories (mirrors the filter dropdown logic).
  const activeSubcategories = subcategories.filter((s) => !s.superseded_by);

  const finish = useCallback((message: string) => {
    // Surface the detail briefly, then hard-reload to re-pull the cell list.
    setStatus({ kind: "ok", message: `${message} Refreshing…` });
    window.location.reload();
  }, []);

  const fail = useCallback((err: unknown) => {
    const message =
      err instanceof Error ? err.message : "Something went wrong. Please try again.";
    setStatus({ kind: "err", message });
    setBusy(false);
  }, []);

  // ── Handlers ───────────────────────────────────────────────────────────────
  const onAddSubcategory = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      if (!engagementId || busy) return;
      if (!family.trim() || !subName.trim()) {
        setStatus({ kind: "err", message: "Family and name are both required." });
        return;
      }
      setBusy(true);
      setStatus(null);
      try {
        const res = await api.addSubcategory(engagementId, {
          family: family.trim(),
          name: subName.trim(),
          hs_codes: splitCodes(hsCodes),
        });
        finish(res.detail || `Added “${subName.trim()}” (+${res.cells_added} cells).`);
      } catch (err) {
        fail(err);
      }
    },
    [engagementId, busy, family, subName, hsCodes, finish, fail],
  );

  const onAddGeography = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      if (!engagementId || busy) return;
      if (!country.trim()) {
        setStatus({ kind: "err", message: "Country is required." });
        return;
      }
      setBusy(true);
      setStatus(null);
      try {
        const res = await api.addGeography(engagementId, { country: country.trim() });
        finish(res.detail || `Added “${country.trim()}” (+${res.cells_added} cells).`);
      } catch (err) {
        fail(err);
      }
    },
    [engagementId, busy, country, finish, fail],
  );

  const onRemoveSubcategory = useCallback(
    async (sub: TaxonomySubcategory) => {
      if (!engagementId || busy) return;
      const ok = window.confirm(
        `Remove “${sub.name}” from the engagement scope?\n\n` +
          "Removing this will delete its cells (and any data in them). " +
          "This cannot be undone.",
      );
      if (!ok) return;
      setBusy(true);
      setStatus(null);
      try {
        const res = await api.removeSubcategory(engagementId, sub.subcategory_id);
        finish(res.detail || `Removed “${sub.name}” (−${res.cells_removed} cells).`);
      } catch (err) {
        fail(err);
      }
    },
    [engagementId, busy, finish, fail],
  );

  const onRemoveGeography = useCallback(
    async (geo: Geography) => {
      if (!engagementId || busy) return;
      const label = `${geo.country} · ${segmentLabel(geo.segment)}`;
      const ok = window.confirm(
        `Remove “${label}” from the engagement scope?\n\n` +
          "Removing this will delete its cells (and any data in them). " +
          "This cannot be undone.",
      );
      if (!ok) return;
      setBusy(true);
      setStatus(null);
      try {
        const res = await api.removeGeography(engagementId, geo.geography_id);
        finish(res.detail || `Removed “${label}” (−${res.cells_removed} cells).`);
      } catch (err) {
        fail(err);
      }
    },
    [engagementId, busy, finish, fail],
  );

  const disabled = busy || !engagementId;

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div className="card overflow-hidden">
      {/* Collapsible header toggle */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="focusable flex w-full items-center justify-between gap-3 px-4 py-3
                   text-left transition-colors hover:bg-surface-subtle"
      >
        <span className="flex flex-col">
          <span className="text-sm font-medium text-ink">
            Edit scope (add / remove subcategories &amp; geographies)
          </span>
          <span className="text-xs text-ink-muted">
            Changing scope adds or removes market cells for this engagement.
          </span>
        </span>
        <span
          aria-hidden
          className={`text-ink-subtle transition-transform ${open ? "rotate-180" : ""}`}
        >
          ▾
        </span>
      </button>

      {open && (
        <div className="space-y-5 border-t border-line px-4 py-4">
          <StatusBanner status={status} />

          {!engagementId && (
            <p className="text-sm text-ink-muted">Loading engagement…</p>
          )}

          {/* ── Add row ──────────────────────────────────────────────── */}
          <div className="grid gap-5 lg:grid-cols-2">
            {/* Add subcategory */}
            <form
              onSubmit={onAddSubcategory}
              className="space-y-3 rounded-lg border border-line bg-surface-subtle/40 p-4"
            >
              <h3 className="text-sm font-semibold text-ink">Add subcategory</h3>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field id="scope-family" label="Family">
                  <input
                    id="scope-family"
                    type="text"
                    value={family}
                    onChange={(e) => setFamily(e.target.value)}
                    placeholder="e.g. Beverages"
                    disabled={disabled}
                    className={INPUT_CLASS}
                  />
                </Field>
                <Field id="scope-subname" label="Name">
                  <input
                    id="scope-subname"
                    type="text"
                    value={subName}
                    onChange={(e) => setSubName(e.target.value)}
                    placeholder="e.g. Energy drinks"
                    disabled={disabled}
                    className={INPUT_CLASS}
                  />
                </Field>
              </div>
              <Field id="scope-hscodes" label="HS codes (comma-separated, optional)">
                <input
                  id="scope-hscodes"
                  type="text"
                  value={hsCodes}
                  onChange={(e) => setHsCodes(e.target.value)}
                  placeholder="2202.10, 2202.99"
                  disabled={disabled}
                  className={INPUT_CLASS}
                />
              </Field>
              <div className="flex justify-end">
                <PrimaryButton disabled={disabled}>
                  {busy ? "Working…" : "Add subcategory"}
                </PrimaryButton>
              </div>
            </form>

            {/* Add geography */}
            <form
              onSubmit={onAddGeography}
              className="space-y-3 rounded-lg border border-line bg-surface-subtle/40 p-4"
            >
              <h3 className="text-sm font-semibold text-ink">Add geography</h3>
              <Field id="scope-country" label="Country">
                <input
                  id="scope-country"
                  type="text"
                  value={country}
                  onChange={(e) => setCountry(e.target.value)}
                  placeholder="e.g. Germany"
                  disabled={disabled}
                  className={INPUT_CLASS}
                />
              </Field>
              <div className="flex justify-end">
                <PrimaryButton disabled={disabled}>
                  {busy ? "Working…" : "Add geography"}
                </PrimaryButton>
              </div>
            </form>
          </div>

          {/* ── Remove row ───────────────────────────────────────────── */}
          <div className="grid gap-5 lg:grid-cols-2">
            {/* Current subcategories */}
            <div className="space-y-2">
              <h3 className="eyebrow">Current subcategories</h3>
              {activeSubcategories.length === 0 ? (
                <p className="text-sm text-ink-subtle">No subcategories yet.</p>
              ) : (
                <ul className="flex flex-wrap gap-2">
                  {activeSubcategories
                    .slice()
                    .sort((a, b) => a.name.localeCompare(b.name))
                    .map((s) => (
                      <li key={s.subcategory_id}>
                        <span
                          className="inline-flex items-center gap-1.5 rounded-full border
                                     border-line bg-surface px-3 py-1 text-xs font-medium text-ink"
                        >
                          {s.name}
                          <button
                            type="button"
                            onClick={() => onRemoveSubcategory(s)}
                            disabled={disabled}
                            aria-label={`Remove subcategory ${s.name}`}
                            title="Remove — deletes its cells"
                            className="focusable -mr-1 rounded-full px-1 text-ink-subtle
                                       transition-colors hover:text-red-600 disabled:opacity-40
                                       disabled:cursor-not-allowed"
                          >
                            ×
                          </button>
                        </span>
                      </li>
                    ))}
                </ul>
              )}
            </div>

            {/* Current geographies */}
            <div className="space-y-2">
              <h3 className="eyebrow">Current geographies</h3>
              {geographies.length === 0 ? (
                <p className="text-sm text-ink-subtle">No geographies yet.</p>
              ) : (
                <ul className="flex flex-wrap gap-2">
                  {geographies
                    .slice()
                    .sort(
                      (a, b) =>
                        a.country.localeCompare(b.country) ||
                        a.segment.localeCompare(b.segment),
                    )
                    .map((g) => (
                      <li key={g.geography_id}>
                        <span
                          className="inline-flex items-center gap-1.5 rounded-full border
                                     border-line bg-surface px-3 py-1 text-xs font-medium text-ink"
                        >
                          {g.country} · {segmentLabel(g.segment)}
                          <button
                            type="button"
                            onClick={() => onRemoveGeography(g)}
                            disabled={disabled}
                            aria-label={`Remove geography ${g.country}`}
                            title="Remove — deletes its cells"
                            className="focusable -mr-1 rounded-full px-1 text-ink-subtle
                                       transition-colors hover:text-red-600 disabled:opacity-40
                                       disabled:cursor-not-allowed"
                          >
                            ×
                          </button>
                        </span>
                      </li>
                    ))}
                </ul>
              )}
            </div>
          </div>

          <p className="text-xs text-ink-subtle">
            Removing a subcategory or geography deletes its market cells and any data
            in them. This cannot be undone.
          </p>
        </div>
      )}
    </div>
  );
}
