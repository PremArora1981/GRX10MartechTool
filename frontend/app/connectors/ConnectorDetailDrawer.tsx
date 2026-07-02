"use client";

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ConnectorHealthBadge } from "@/components/ConnectorHealthBadge";
import { api } from "@/lib/api";
import { formatTimestamp } from "@/lib/format";
import type { Source } from "@/lib/types";

// ---------------------------------------------------------------------------
// Access-method + class badge helpers (shared within this file)
// ---------------------------------------------------------------------------

const ACCESS_METHOD_STYLES: Record<string, { label: string; cls: string }> = {
  api: { label: "API", cls: "bg-brand-50 text-brand-700" },
  scrape: { label: "Scrape", cls: "bg-orange-50 text-orange-700" },
  web_search: { label: "Web search", cls: "bg-blue-50 text-blue-700" },
  manual_upload: { label: "Manual upload", cls: "bg-slate-100 text-slate-600" },
};

const CLASS_STYLES: Record<string, { label: string; cls: string }> = {
  A: {
    label: "A — Primary structured",
    cls: "bg-emerald-50 text-emerald-700",
  },
  B: {
    label: "B — Industry/procedural",
    cls: "bg-amber-50 text-amber-700",
  },
  C: {
    label: "C — Triangulation support",
    cls: "bg-slate-100 text-slate-600",
  },
};

function AccessBadge({ method }: { method: string | null }) {
  const s = method ? (ACCESS_METHOD_STYLES[method] ?? ACCESS_METHOD_STYLES.api) : null;
  if (!s) return <span className="text-ink-subtle">—</span>;
  return (
    <span className={`badge ${s.cls}`}>
      {method === "scrape" && (
        <span aria-hidden className="mr-0.5">
          ⚠
        </span>
      )}
      {s.label}
    </span>
  );
}

function ClassBadge({ cls }: { cls: string | null }) {
  const s = cls ? (CLASS_STYLES[cls] ?? null) : null;
  if (!s) return <span className="text-ink-subtle">—</span>;
  return <span className={`badge ${s.cls}`}>{s.label}</span>;
}

// ---------------------------------------------------------------------------
// Definition list row
// ---------------------------------------------------------------------------

function Dl({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[10rem_1fr] gap-2 py-2 border-b border-line last:border-0">
      <dt className="text-xs font-medium text-ink-subtle self-start pt-0.5">
        {label}
      </dt>
      <dd className="text-sm text-ink break-words">{children}</dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Credential section (write-only, owner_admin gated)
// ---------------------------------------------------------------------------

interface CredentialSectionProps {
  source: Source;
}

function CredentialSection({ source }: CredentialSectionProps) {
  const [secret, setSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const needsCredential = source.auth && source.auth !== "none";

  if (!needsCredential) {
    return (
      <p className="text-sm text-ink-muted">
        This source uses <strong>no authentication</strong> — no credential
        required.
      </p>
    );
  }

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    if (!secret.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const res = await api.setCredential(source.source_id, secret.trim());
      setSavedAt(res.rotated_at);
      setSecret("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save credential");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-3">
      {/* Status indicator */}
      <div className="flex items-center gap-2">
        {source.auth_secret_ref || savedAt ? (
          <span className="badge bg-emerald-50 text-emerald-700">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden />
            Credential set
          </span>
        ) : (
          <span className="badge bg-red-50 text-red-700">
            <span className="h-1.5 w-1.5 rounded-full bg-red-500" aria-hidden />
            No credential
          </span>
        )}
        {savedAt && (
          <span className="text-xs text-ink-subtle">
            Rotated {formatTimestamp(savedAt)}
          </span>
        )}
      </div>

      {/* Write-only entry form */}
      <form onSubmit={handleSave} className="space-y-2">
        <label className="block">
          <span className="text-xs font-medium text-ink-subtle">
            {source.auth_secret_ref || savedAt
              ? "Rotate credential (enter new secret)"
              : "Enter credential secret"}
          </span>
          <input
            type="password"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            autoComplete="new-password"
            placeholder="Paste API key, token, or password…"
            className="mt-1 w-full rounded-lg border border-line bg-white px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-brand/40"
            disabled={saving}
          />
        </label>

        <p className="text-2xs text-ink-subtle">
          The secret is envelope-encrypted on the server (pgcrypto). It is
          never returned to the browser. The rotation timestamp is logged for
          audit.
        </p>

        {error && (
          <p className="text-xs text-red-600 bg-red-50 rounded-lg px-3 py-2">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={saving || !secret.trim()}
          className="btn-primary text-sm px-4 py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? "Saving…" : "Save credential"}
        </button>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main drawer component
// ---------------------------------------------------------------------------

export interface ConnectorDetailDrawerProps {
  source: Source;
  canEnterCredentials: boolean;
  onClose: () => void;
  onSourceUpdated: (updated: Source) => void;
}

export function ConnectorDetailDrawer({
  source,
  canEnterCredentials,
  onClose,
  onSourceUpdated,
}: ConnectorDetailDrawerProps) {
  const [probing, setProbing] = useState(false);
  const [probeError, setProbeError] = useState<string | null>(null);
  const [togglingEnabled, setTogglingEnabled] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);

  // Trap focus + Escape to close.
  useEffect(() => {
    closeRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Lock body scroll while drawer is open.
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  async function handleProbe() {
    setProbing(true);
    setProbeError(null);
    try {
      const updated = await api.probeSource(source.source_id);
      onSourceUpdated(updated);
    } catch (err) {
      setProbeError(
        err instanceof Error ? err.message : "Probe failed — see logs.",
      );
    } finally {
      setProbing(false);
    }
  }

  async function handleToggleEnabled() {
    setTogglingEnabled(true);
    try {
      const updated = await (source.enabled
        ? api.disableSource(source.source_id)
        : api.enableSource(source.source_id));
      onSourceUpdated(updated);
    } finally {
      setTogglingEnabled(false);
    }
  }

  return (
    <>
      {/* Overlay */}
      <div
        className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm"
        aria-hidden
        onClick={onClose}
      />

      {/* Drawer panel */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`${source.publisher} connector detail`}
        className="fixed right-0 top-0 z-50 flex h-full w-full max-w-[480px] flex-col bg-white shadow-2xl"
      >
        {/* Header */}
        <div className="flex shrink-0 items-start justify-between border-b border-line px-5 py-4">
          <div className="min-w-0">
            <p className="font-mono text-xs text-ink-subtle">{source.source_id}</p>
            <h2 className="mt-0.5 text-base font-semibold text-ink leading-tight">
              {source.publisher}
            </h2>
          </div>
          <button
            ref={closeRef}
            onClick={onClose}
            className="focusable ml-4 shrink-0 rounded-lg p-1.5 text-ink-subtle hover:bg-surface-subtle hover:text-ink"
            aria-label="Close drawer"
          >
            <svg
              viewBox="0 0 24 24"
              className="h-5 w-5"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.8}
              aria-hidden
            >
              <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        {/* Scrollable content */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-6">
          {/* Health section */}
          <section aria-label="Connector health">
            <div className="eyebrow mb-3">Connector health</div>
            <div className="card bg-surface-subtle px-4 py-4 space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <ConnectorHealthBadge
                  status={source.last_probe_status}
                  budgetWarning={source.budget_warning}
                  detail={source.last_probe_detail ?? undefined}
                />
                {!source.enabled && (
                  <span className="badge bg-slate-100 text-slate-500">
                    Disabled
                  </span>
                )}
              </div>

              {source.last_probe_at && (
                <p className="text-xs text-ink-subtle">
                  Last probed: {formatTimestamp(source.last_probe_at)}
                </p>
              )}

              {source.last_probe_detail && (
                <p className="text-xs text-ink-muted bg-white rounded border border-line px-3 py-2 font-mono break-all">
                  {source.last_probe_detail}
                </p>
              )}

              {probeError && (
                <p className="text-xs text-red-600 bg-red-50 rounded-lg px-3 py-2">
                  {probeError}
                </p>
              )}

              <div className="flex gap-2">
                <button
                  onClick={handleProbe}
                  disabled={probing}
                  className="btn-primary text-xs px-3 py-1.5 disabled:opacity-50"
                >
                  {probing ? "Probing…" : "Probe now"}
                </button>

                {canEnterCredentials && (
                  <button
                    onClick={handleToggleEnabled}
                    disabled={togglingEnabled}
                    className="btn-secondary text-xs px-3 py-1.5 disabled:opacity-50"
                  >
                    {togglingEnabled
                      ? "Updating…"
                      : source.enabled
                        ? "Disable"
                        : "Enable"}
                  </button>
                )}
              </div>
            </div>
          </section>

          {/* Metadata */}
          <section aria-label="Source metadata">
            <div className="eyebrow mb-3">Metadata</div>
            <dl className="rounded-xl border border-line bg-white px-4">
              <Dl label="Class">
                <ClassBadge cls={source.class} />
              </Dl>
              <Dl label="Access method">
                <AccessBadge method={source.access_method} />
              </Dl>
              <Dl label="Auth">
                {source.auth ? (
                  <span className="font-mono text-xs">{source.auth}</span>
                ) : (
                  <span className="text-ink-subtle">—</span>
                )}
              </Dl>
              <Dl label="Raw table">
                {source.raw_table ? (
                  <span className="font-mono text-xs text-brand">
                    {source.raw_table}
                  </span>
                ) : (
                  <span className="text-ink-subtle">—</span>
                )}
              </Dl>
              <Dl label="Connector module">
                {source.connector ? (
                  <span className="font-mono text-xs">{source.connector}</span>
                ) : (
                  <span className="text-ink-subtle">—</span>
                )}
              </Dl>
              <Dl label="Refresh cadence">
                {source.refresh_cadence ?? (
                  <span className="text-ink-subtle">—</span>
                )}
              </Dl>
              <Dl label="URL pattern">
                {source.url_pattern ? (
                  <span className="font-mono text-xs break-all text-ink-muted">
                    {source.url_pattern}
                  </span>
                ) : (
                  <span className="text-ink-subtle">—</span>
                )}
              </Dl>
              {source.monthly_budget !== null && (
                <Dl label="Monthly budget">
                  <span
                    className={
                      source.budget_warning
                        ? "text-orange-700 font-medium"
                        : undefined
                    }
                  >
                    ${source.monthly_budget.toFixed(2)}/mo
                    {source.budget_warning && (
                      <span className="ml-1.5 badge bg-orange-50 text-orange-700 text-2xs">
                        ≥80% used
                      </span>
                    )}
                  </span>
                </Dl>
              )}
              {source.quota_ceiling !== null && (
                <Dl label="Quota ceiling">
                  <span
                    className={
                      source.budget_warning
                        ? "text-orange-700 font-medium"
                        : undefined
                    }
                  >
                    {source.quota_ceiling.toLocaleString()} calls/period
                    {source.budget_warning && (
                      <span className="ml-1.5 badge bg-orange-50 text-orange-700 text-2xs">
                        ≥80% used
                      </span>
                    )}
                  </span>
                </Dl>
              )}
              {source.notes && (
                <Dl label="Notes">
                  <span className="text-ink-muted">{source.notes}</span>
                </Dl>
              )}
            </dl>
          </section>

          {/* Credential section (admin only) */}
          {canEnterCredentials && (
            <section aria-label="Credential management">
              <div className="eyebrow mb-3">Credential</div>
              <div className="card px-4 py-4">
                <CredentialSection source={source} />
              </div>
            </section>
          )}
        </div>
      </div>
    </>
  );
}

export default ConnectorDetailDrawer;
