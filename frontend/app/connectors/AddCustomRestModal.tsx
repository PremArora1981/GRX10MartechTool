"use client";

import { useEffect, useRef, useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";
import { api } from "@/lib/api";
import type {
  AddSourcePayload,
  FieldMapping,
  Source,
  SourceClass,
} from "@/lib/types";

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

/** Typed columns per raw table (mirrors the DDL in changelog-master.sql). */
const RAW_TABLE_COLUMNS: Record<string, string[]> = {
  raw_trade_flows: ["reporter", "partner", "hs_code", "hs_version", "flow", "period", "value_usd", "qty", "qty_unit"],
  raw_regulatory: ["registration_id", "holder", "product_code", "country", "status"],
  raw_filings: ["filer", "ticker", "period", "segment", "geography", "revenue_usd", "doc_url"],
  raw_transcripts: ["company", "period", "content"],
  raw_shipments: ["shipper", "consignee", "hs_code", "origin", "dest", "value_usd", "period"],
  raw_external_metrics: ["indicator", "country", "period", "value", "unit"],
  raw_industry_reports: ["publisher", "market", "period", "tam_usd", "doc_url"],
  raw_patents: ["patent_id", "assignee", "cpc", "filing_date", "country"],
  raw_procurement: ["award_id", "buyer", "supplier", "country", "value_usd", "period"],
  raw_standards: ["body", "member", "membership_tier"],
  raw_news: ["headline", "url", "published_at", "entity", "snippet"],
  raw_signals: ["company", "signal_type", "country", "period", "value"],
};

const AUTH_OPTIONS = [
  { value: "none", label: "None (public endpoint)" },
  { value: "api_key", label: "API key (header / query param)" },
  { value: "oauth", label: "OAuth 2.0 (client credentials)" },
  { value: "login", label: "Login / session-based" },
];

const CLASS_OPTIONS: { value: SourceClass; label: string }[] = [
  { value: "A", label: "A — Primary structured (qualifies HIGH confidence)" },
  { value: "B", label: "B — Industry / procedural (qualifies MEDIUM)" },
  { value: "C", label: "C — Triangulation support (gap-fill only)" },
];

const CADENCE_OPTIONS = [
  "daily", "weekly", "monthly", "quarterly", "on-demand",
];

// ---------------------------------------------------------------------------
// Form state
// ---------------------------------------------------------------------------

interface FormState {
  source_id: string;
  publisher: string;
  url_pattern: string;
  auth: string;
  cls: SourceClass;
  connector: string;
  refresh_cadence: string;
  raw_table: string;
  monthly_budget: string;
  quota_ceiling: string;
  notes: string;
  sample_response: string;
}

const EMPTY_FORM: FormState = {
  source_id: "",
  publisher: "",
  url_pattern: "",
  auth: "api_key",
  cls: "B",
  connector: "generic_rest",
  refresh_cadence: "weekly",
  raw_table: "",
  monthly_budget: "",
  quota_ceiling: "",
  notes: "",
  sample_response: "",
};

function slug(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

// ---------------------------------------------------------------------------
// Field-mapping editor row
// ---------------------------------------------------------------------------

interface MappingRowProps {
  mapping: FieldMapping;
  rawTable: string;
  onChange: (updated: FieldMapping) => void;
  onDelete: () => void;
}

function MappingRow({ mapping, rawTable, onChange, onDelete }: MappingRowProps) {
  const cols = rawTable ? (RAW_TABLE_COLUMNS[rawTable] ?? []) : [];
  return (
    <tr className="border-b border-line last:border-0">
      <td className="px-3 py-2">
        <input
          value={mapping.raw_field}
          onChange={(e) => onChange({ ...mapping, raw_field: e.target.value })}
          className="w-full font-mono text-xs rounded border border-line px-2 py-1 focus:outline-none focus:ring-1 focus:ring-brand/40"
          placeholder="data[].value"
        />
      </td>
      <td className="px-3 py-2">
        <select
          value={mapping.mapped_column}
          onChange={(e) => onChange({ ...mapping, mapped_column: e.target.value })}
          className="w-full text-xs rounded border border-line px-2 py-1 focus:outline-none focus:ring-1 focus:ring-brand/40"
        >
          <option value="">— pick column —</option>
          {cols.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
          <option value="__custom">Custom…</option>
        </select>
      </td>
      <td className="px-3 py-2">
        <input
          value={mapping.transform ?? ""}
          onChange={(e) => onChange({ ...mapping, transform: e.target.value || null })}
          className="w-full font-mono text-xs rounded border border-line px-2 py-1 focus:outline-none focus:ring-1 focus:ring-brand/40"
          placeholder="e.g. parse_iso_date"
        />
      </td>
      <td className="px-3 py-2">
        <span className="text-xs text-ink-subtle">{mapping.notes ?? ""}</span>
      </td>
      <td className="px-3 py-2 text-center">
        <button
          type="button"
          onClick={onDelete}
          className="text-red-400 hover:text-red-600"
          aria-label="Remove mapping"
        >
          ✕
        </button>
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Main modal
// ---------------------------------------------------------------------------

export interface AddCustomRestModalProps {
  onClose: () => void;
  onSuccess: (added: Source) => void;
}

export function AddCustomRestModal({ onClose, onSuccess }: AddCustomRestModalProps) {
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [mappings, setMappings] = useState<FieldMapping[]>([]);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestError, setSuggestError] = useState<string | null>(null);
  const [suggestConfidence, setSuggestConfidence] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [errors, setErrors] = useState<Partial<Record<keyof FormState, string>>>({});
  const firstInputRef = useRef<HTMLInputElement>(null);

  // Focus first field + Escape to close.
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
      next.url_pattern = "URL pattern is required for REST sources.";
    if (!form.raw_table)
      next.raw_table = "Select the target raw table.";
    setErrors(next);
    return Object.keys(next).length === 0;
  }

  async function handleSuggestMapping() {
    if (!form.url_pattern.trim() || !form.raw_table) {
      setSuggestError("Fill in URL pattern and raw table first.");
      return;
    }
    setSuggesting(true);
    setSuggestError(null);
    try {
      const res = await api.suggestMapping({
        url_pattern: form.url_pattern.trim(),
        raw_table: form.raw_table,
        sample_response: form.sample_response.trim() || null,
      });
      setMappings(res.mappings);
      setSuggestConfidence(res.confidence);
    } catch (err) {
      setSuggestError(
        err instanceof Error ? err.message : "Suggest mapping failed.",
      );
    } finally {
      setSuggesting(false);
    }
  }

  function addBlankMapping() {
    setMappings((prev) => [
      ...prev,
      { raw_field: "", mapped_column: "", transform: null, notes: null },
    ]);
  }

  function updateMapping(i: number, updated: FieldMapping) {
    setMappings((prev) => prev.map((m, idx) => (idx === i ? updated : m)));
  }

  function deleteMapping(i: number) {
    setMappings((prev) => prev.filter((_, idx) => idx !== i));
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
        auth: form.auth,
        class: form.cls,
        connector: form.connector.trim() || "generic_rest",
        refresh_cadence: form.refresh_cadence || null,
        raw_table: form.raw_table || null,
        access_method: "api",
        monthly_budget: form.monthly_budget ? parseFloat(form.monthly_budget) : null,
        quota_ceiling: form.quota_ceiling ? parseInt(form.quota_ceiling, 10) : null,
        notes: form.notes.trim() || null,
        field_mappings: mappings.filter(
          (m) => m.raw_field.trim() && m.mapped_column,
        ),
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

  const canSuggest =
    form.url_pattern.trim().length > 0 && form.raw_table.length > 0;

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
        aria-label="Add custom REST source"
        className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:p-8"
      >
        <div
          className="relative w-full max-w-2xl rounded-2xl bg-white shadow-2xl my-auto"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Modal header */}
          <div className="flex items-center justify-between border-b border-line px-6 py-4">
            <div>
              <h2 className="text-base font-semibold text-ink">
                Add custom REST source
              </h2>
              <p className="mt-0.5 text-xs text-ink-muted">
                Registers a new API endpoint with the generic-REST connector.
                Use the AI-assisted mapper to suggest field mappings.
              </p>
            </div>
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

          {/* Form body */}
          <form onSubmit={handleSubmit} noValidate>
            <div className="px-6 py-5 space-y-5">
              {/* Row 1: publisher + source_id */}
              <div className="grid grid-cols-2 gap-4">
                <Field
                  label="Publisher name"
                  required
                  error={errors.publisher}
                >
                  <input
                    ref={firstInputRef}
                    type="text"
                    value={form.publisher}
                    onChange={(e) => set("publisher", e.target.value)}
                    onBlur={handlePublisherBlur}
                    placeholder="e.g. World Bank"
                    className={inputCls(!!errors.publisher)}
                  />
                </Field>
                <Field
                  label="Source ID"
                  hint="Unique slug: lowercase + underscores"
                  required
                  error={errors.source_id}
                >
                  <input
                    type="text"
                    value={form.source_id}
                    onChange={(e) => set("source_id", slug(e.target.value))}
                    placeholder="e.g. world_bank_indicators"
                    className={`${inputCls(!!errors.source_id)} font-mono`}
                  />
                </Field>
              </div>

              {/* Row 2: URL pattern */}
              <Field
                label="URL pattern"
                hint="Use {param} placeholders for variable parts"
                required
                error={errors.url_pattern}
              >
                <input
                  type="url"
                  value={form.url_pattern}
                  onChange={(e) => set("url_pattern", e.target.value)}
                  placeholder="https://api.example.com/v1/data?country={country}&year={year}"
                  className={`${inputCls(!!errors.url_pattern)} font-mono text-xs`}
                />
              </Field>

              {/* Row 3: auth + class */}
              <div className="grid grid-cols-2 gap-4">
                <Field label="Authentication">
                  <select
                    value={form.auth}
                    onChange={(e) => set("auth", e.target.value)}
                    className={inputCls(false)}
                  >
                    {AUTH_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Confidence class">
                  <select
                    value={form.cls}
                    onChange={(e) => set("cls", e.target.value as SourceClass)}
                    className={inputCls(false)}
                  >
                    {CLASS_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </Field>
              </div>

              {/* Row 4: raw_table + cadence */}
              <div className="grid grid-cols-2 gap-4">
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
              </div>

              {/* Row 5: budget / quota */}
              <div className="grid grid-cols-2 gap-4">
                <Field label="Monthly budget ceiling (USD)" hint="Optional — triggers 🟠 warning at 80%">
                  <input
                    type="number"
                    min={0}
                    step={0.01}
                    value={form.monthly_budget}
                    onChange={(e) => set("monthly_budget", e.target.value)}
                    placeholder="e.g. 50.00"
                    className={inputCls(false)}
                  />
                </Field>
                <Field label="Quota ceiling (calls/period)" hint="Optional">
                  <input
                    type="number"
                    min={0}
                    value={form.quota_ceiling}
                    onChange={(e) => set("quota_ceiling", e.target.value)}
                    placeholder="e.g. 500"
                    className={inputCls(false)}
                  />
                </Field>
              </div>

              {/* Notes */}
              <Field label="Notes">
                <textarea
                  value={form.notes}
                  onChange={(e) => set("notes", e.target.value)}
                  rows={2}
                  placeholder="Rate limits, HS-code version, known quirks…"
                  className={`${inputCls(false)} resize-y`}
                />
              </Field>

              {/* AI Suggest Mapping section */}
              <div className="rounded-xl border border-brand/30 bg-brand-50 p-4 space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-sm font-semibold text-brand-700">
                      AI-assisted field mapping
                    </div>
                    <p className="text-xs text-brand-700/70 mt-0.5">
                      Optionally paste a sample JSON response — Claude will
                      suggest how to map each field to{" "}
                      {form.raw_table || "the target raw table"} columns.
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={handleSuggestMapping}
                    disabled={suggesting || !canSuggest}
                    className="shrink-0 rounded-lg bg-brand px-4 py-2 text-xs font-semibold text-white disabled:opacity-50 hover:bg-brand-700 transition-colors"
                  >
                    {suggesting ? "Suggesting…" : "Suggest mapping"}
                  </button>
                </div>

                <textarea
                  value={form.sample_response}
                  onChange={(e: ChangeEvent<HTMLTextAreaElement>) =>
                    set("sample_response", e.target.value)
                  }
                  rows={3}
                  placeholder='Paste a sample JSON response here… {"data": [{"country": "JP", "value": 12345}]}'
                  className="w-full rounded-lg border border-brand/20 bg-white px-3 py-2 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-brand/30 resize-y"
                />

                {suggestError && (
                  <p className="text-xs text-red-600 bg-red-50 rounded px-3 py-2">
                    {suggestError}
                  </p>
                )}

                {suggestConfidence && (
                  <p className="text-xs text-brand-700">
                    AI confidence in these mappings:{" "}
                    <strong>{suggestConfidence}</strong>. Review and adjust
                    before saving.
                  </p>
                )}
              </div>

              {/* Mapping editor table */}
              {(mappings.length > 0 || form.raw_table) && (
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <div className="eyebrow">Field mappings</div>
                    <button
                      type="button"
                      onClick={addBlankMapping}
                      className="text-xs text-brand hover:underline"
                    >
                      + Add row
                    </button>
                  </div>
                  {mappings.length === 0 ? (
                    <p className="text-xs text-ink-subtle text-center py-4 border border-dashed border-line rounded-xl">
                      No mappings yet — use "Suggest mapping" or add rows
                      manually.
                    </p>
                  ) : (
                    <div className="rounded-xl border border-line overflow-hidden">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="bg-surface-subtle border-b border-line">
                            <th className="eyebrow text-left px-3 py-2 w-[30%]">
                              JSON path
                            </th>
                            <th className="eyebrow text-left px-3 py-2 w-[28%]">
                              Column
                            </th>
                            <th className="eyebrow text-left px-3 py-2 w-[22%]">
                              Transform
                            </th>
                            <th className="eyebrow text-left px-3 py-2 w-[15%]">
                              Notes
                            </th>
                            <th className="w-8" />
                          </tr>
                        </thead>
                        <tbody>
                          {mappings.map((m, i) => (
                            <MappingRow
                              key={i}
                              mapping={m}
                              rawTable={form.raw_table}
                              onChange={(u) => updateMapping(i, u)}
                              onDelete={() => deleteMapping(i)}
                            />
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
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
                disabled={submitting}
                className="btn-primary text-sm px-5 py-2 disabled:opacity-50"
              >
                {submitting ? "Adding source…" : "Add source"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Micro helpers
// ---------------------------------------------------------------------------

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

export default AddCustomRestModal;
