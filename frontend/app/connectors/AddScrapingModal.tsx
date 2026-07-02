"use client";

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { api } from "@/lib/api";
import type { AddSourcePayload, Source, SourceClass } from "@/lib/types";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const RAW_TABLES = [
  "raw_trade_flows",
  "raw_regulatory",
  "raw_filings",
  "raw_transcripts",
  "raw_shipments",
  "raw_external_metrics",
  "raw_industry_reports",
  "raw_patents",
  "raw_procurement",
  "raw_standards",
  "raw_news",
  "raw_signals",
] as const;

/**
 * Scraping sources cap at class B — class A requires structured, guaranteed
 * programmatic access which scraping cannot provide.
 */
const CLASS_OPTIONS: { value: SourceClass; label: string }[] = [
  { value: "B", label: "B — Industry / procedural (qualifies MEDIUM)" },
  { value: "C", label: "C — Triangulation support (gap-fill only)" },
];

const CADENCE_OPTIONS = ["daily", "weekly", "monthly", "quarterly", "on-demand"];

// ---------------------------------------------------------------------------
// Form state
// ---------------------------------------------------------------------------

interface FormState {
  source_id: string;
  publisher: string;
  url_pattern: string;
  cls: SourceClass;
  connector: string;
  refresh_cadence: string;
  raw_table: string;
  notes: string;
}

const EMPTY: FormState = {
  source_id: "",
  publisher: "",
  url_pattern: "",
  cls: "C",
  connector: "generic_scrape",
  refresh_cadence: "weekly",
  raw_table: "",
  notes: "",
};

function slug(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

function inputCls(error: boolean) {
  return `w-full rounded-lg border ${
    error ? "border-red-400 focus:ring-red-300" : "border-line focus:ring-brand/40"
  } bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2`;
}

function Field({
  label,
  hint,
  required,
  error,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  error?: string;
  children: ReactNode;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-ink-subtle">
        {label}
        {required && <span className="text-red-500 ml-0.5">*</span>}
        {hint && (
          <span className="ml-1.5 font-normal text-ink-subtle/70">{hint}</span>
        )}
      </span>
      {children}
      {error && <span className="text-xs text-red-600">{error}</span>}
    </label>
  );
}

// ---------------------------------------------------------------------------
// Main modal
// ---------------------------------------------------------------------------

export interface AddScrapingModalProps {
  onClose: () => void;
  onSuccess: (added: Source) => void;
}

export function AddScrapingModal({ onClose, onSuccess }: AddScrapingModalProps) {
  const [form, setForm] = useState<FormState>(EMPTY);
  const [errors, setErrors] = useState<Partial<Record<keyof FormState, string>>>({});
  const [submitting, setSubmitting] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const firstInputRef = useRef<HTMLInputElement>(null);

  // Focus + Escape handler.
  useEffect(() => {
    firstInputRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Lock body scroll.
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, []);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
    setErrors((prev) => ({ ...prev, [key]: undefined }));
  }

  function handlePublisherBlur() {
    if (!form.source_id && form.publisher) {
      set("source_id", slug(form.publisher));
    }
  }

  function validate(): boolean {
    const next: Partial<Record<keyof FormState, string>> = {};
    if (!form.source_id.trim())
      next.source_id = "Source ID is required.";
    else if (!/^[a-z][a-z0-9_]*$/.test(form.source_id.trim()))
      next.source_id = "Use lowercase letters, digits, and underscores only.";
    if (!form.publisher.trim())
      next.publisher = "Publisher name is required.";
    if (!form.url_pattern.trim())
      next.url_pattern = "Target URL is required.";
    if (!form.raw_table)
      next.raw_table = "Select the target raw table.";
    if (!confirmed)
      next.notes = "Acknowledge the fragility warning before adding.";
    setErrors(next);
    return Object.keys(next).length === 0;
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!validate()) return;
    setSubmitting(true);
    try {
      const payload: AddSourcePayload = {
        source_id: form.source_id.trim(),
        publisher: form.publisher.trim(),
        url_pattern: form.url_pattern.trim() || null,
        auth: "none",
        class: form.cls,
        connector: form.connector.trim() || "generic_scrape",
        refresh_cadence: form.refresh_cadence || null,
        raw_table: form.raw_table || null,
        access_method: "scrape",
        monthly_budget: null,
        quota_ceiling: null,
        notes: form.notes.trim() || null,
        field_mappings: [],
      };
      const added = await api.addSource(payload);
      onSuccess(added);
    } catch (err) {
      setErrors({
        source_id: err instanceof Error ? err.message : "Failed to add source.",
      });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm"
        aria-hidden
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Add scraping source"
        className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:p-8"
      >
        <div
          className="relative w-full max-w-xl rounded-2xl bg-white shadow-2xl my-auto"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Modal header */}
          <div className="flex items-center justify-between border-b border-line px-6 py-4">
            <h2 className="text-base font-semibold text-ink flex items-center gap-2">
              <span className="text-orange-500 text-lg" aria-hidden>⚠</span>
              Add scraping source
              <span className="badge bg-orange-50 text-orange-700 text-2xs">
                fragile
              </span>
            </h2>
            <button
              onClick={onClose}
              className="focusable shrink-0 ml-4 rounded-lg p-1.5 text-ink-subtle hover:bg-surface-subtle hover:text-ink"
              aria-label="Close"
            >
              <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden>
                <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round" />
              </svg>
            </button>
          </div>

          {/* Fragility warning banner */}
          <div className="mx-6 mt-5 rounded-xl border border-orange-300 bg-orange-50 px-4 py-4">
            <div className="flex gap-3">
              <span className="text-orange-500 text-xl shrink-0 mt-0.5" aria-hidden>
                ⚠
              </span>
              <div className="space-y-1.5 text-sm text-orange-900">
                <p className="font-semibold">
                  Scraping sources are inherently fragile — read carefully
                  before proceeding.
                </p>
                <ul className="space-y-1 text-xs list-disc list-inside text-orange-800">
                  <li>
                    <strong>No SLA:</strong> the source website can change
                    HTML structure without notice, breaking extraction silently.
                  </li>
                  <li>
                    <strong>ToS risk:</strong> many sites prohibit automated
                    scraping. Verify the target site's Terms of Service.
                  </li>
                  <li>
                    <strong>Geo-block risk:</strong> Render datacenter IPs may
                    be blocked by non-US government sites or CDN WAFs.
                  </li>
                  <li>
                    <strong>Confidence ceiling:</strong> scraping sources are
                    capped at class B (MEDIUM) maximum — they can never qualify
                    a cell for HIGH confidence.
                  </li>
                  <li>
                    <strong>Breakage monitor required:</strong> add a probe()
                    health check so failures surface in the status page before
                    a pipeline run.
                  </li>
                </ul>
                <p className="text-xs text-orange-700 mt-2">
                  Prefer a structured API connector or the OCDS/ATS family
                  connector where available. Only use scraping as a last resort.
                </p>
              </div>
            </div>
          </div>

          {/* Form body */}
          <form onSubmit={handleSubmit} noValidate>
            <div className="px-6 py-5 space-y-5">
              {/* Row 1: publisher + source_id */}
              <div className="grid grid-cols-2 gap-4">
                <Field label="Publisher name" required error={errors.publisher}>
                  <input
                    ref={firstInputRef}
                    type="text"
                    value={form.publisher}
                    onChange={(e) => set("publisher", e.target.value)}
                    onBlur={handlePublisherBlur}
                    placeholder="e.g. China NBS"
                    className={inputCls(!!errors.publisher)}
                  />
                </Field>
                <Field
                  label="Source ID"
                  hint="slug: lowercase + underscores"
                  required
                  error={errors.source_id}
                >
                  <input
                    type="text"
                    value={form.source_id}
                    onChange={(e) => set("source_id", slug(e.target.value))}
                    placeholder="e.g. china_nbs_national_data"
                    className={`${inputCls(!!errors.source_id)} font-mono`}
                  />
                </Field>
              </div>

              {/* Target URL */}
              <Field
                label="Target URL"
                hint="The page or endpoint to scrape"
                required
                error={errors.url_pattern}
              >
                <input
                  type="url"
                  value={form.url_pattern}
                  onChange={(e) => set("url_pattern", e.target.value)}
                  placeholder="https://data.example.gov/page"
                  className={`${inputCls(!!errors.url_pattern)} font-mono text-xs`}
                />
              </Field>

              {/* Class + raw_table */}
              <div className="grid grid-cols-2 gap-4">
                <Field
                  label="Confidence class"
                  hint="Capped at B for scraping"
                >
                  <select
                    value={form.cls}
                    onChange={(e) => set("cls", e.target.value as SourceClass)}
                    className={inputCls(false)}
                  >
                    {CLASS_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field
                  label="Target raw table"
                  required
                  error={errors.raw_table}
                >
                  <select
                    value={form.raw_table}
                    onChange={(e) => set("raw_table", e.target.value)}
                    className={inputCls(!!errors.raw_table)}
                  >
                    <option value="">— select table —</option>
                    {RAW_TABLES.map((t) => (
                      <option key={t} value={t}>{t}</option>
                    ))}
                  </select>
                </Field>
              </div>

              {/* Cadence */}
              <Field label="Refresh cadence">
                <select
                  value={form.refresh_cadence}
                  onChange={(e) => set("refresh_cadence", e.target.value)}
                  className={inputCls(false)}
                >
                  {CADENCE_OPTIONS.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
              </Field>

              {/* Locked fields — shown as read-only for transparency */}
              <div className="rounded-xl border border-line bg-surface-subtle px-4 py-3 space-y-2">
                <div className="eyebrow mb-1">Locked settings for scraping sources</div>
                <div className="grid grid-cols-2 gap-3 text-xs">
                  <div>
                    <span className="text-ink-subtle block">Access method</span>
                    <span className="font-mono text-orange-700">scrape</span>
                  </div>
                  <div>
                    <span className="text-ink-subtle block">Authentication</span>
                    <span className="font-mono">none</span>
                  </div>
                  <div>
                    <span className="text-ink-subtle block">Connector module</span>
                    <span className="font-mono">{form.connector || "generic_scrape"}</span>
                  </div>
                  <div>
                    <span className="text-ink-subtle block">Max confidence cap</span>
                    <span className="font-semibold text-amber-700">MEDIUM (class B)</span>
                  </div>
                </div>
              </div>

              {/* Notes */}
              <Field label="Notes / ToS verification">
                <textarea
                  value={form.notes}
                  onChange={(e) => set("notes", e.target.value)}
                  rows={3}
                  placeholder="Document ToS review outcome, HTML selector patterns, known break points, proxy requirements…"
                  className={`${inputCls(false)} resize-y`}
                />
              </Field>

              {/* Acknowledgement checkbox */}
              <div className={`rounded-xl border px-4 py-3 ${!confirmed && errors.notes ? "border-red-400 bg-red-50" : "border-line bg-surface-subtle"}`}>
                <label className="flex items-start gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={confirmed}
                    onChange={(e) => {
                      setConfirmed(e.target.checked);
                      if (e.target.checked) {
                        setErrors((prev) => ({ ...prev, notes: undefined }));
                      }
                    }}
                    className="mt-0.5 shrink-0 accent-brand"
                  />
                  <span className="text-sm text-ink">
                    I have read the fragility warnings above. I understand this
                    source has no SLA, may break on HTML changes, and is capped
                    at MEDIUM confidence. I have verified or will verify the
                    site's Terms of Service before enabling automated scraping.
                  </span>
                </label>
                {!confirmed && errors.notes && (
                  <p className="mt-2 text-xs text-red-600">{errors.notes}</p>
                )}
              </div>
            </div>

            {/* Footer */}
            <div className="flex items-center justify-between gap-3 border-t border-line px-6 py-4">
              <button
                type="button"
                onClick={onClose}
                className="btn-secondary text-sm px-4 py-2"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={submitting || !confirmed}
                className="rounded-lg bg-orange-600 px-5 py-2 text-sm font-semibold text-white hover:bg-orange-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {submitting ? "Adding source…" : "Add scraping source"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </>
  );
}

export default AddScrapingModal;
