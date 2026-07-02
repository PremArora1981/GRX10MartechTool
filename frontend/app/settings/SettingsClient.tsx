"use client";

/**
 * Settings screen — fully interactive client shell.
 *
 * Three independent sections:
 *   1. Validation Profile — pick from Light/Standard/Conservative/Audit-grade
 *      or clone-and-tweak any profile into a custom one (owner/admin only).
 *   2. Web-Search Fallback — per-engagement toggle (Q8); always Class C / LOW.
 *   3. Audience View — session-level switcher; lets analyst/admin preview the
 *      platform as business or external audiences would see it.
 *
 * Writes go through `apiRequest` directly so mutations stay client-side without
 * a full page reload. Every mutation is optimistic with revert-on-error.
 */

import { useState } from "react";
import type { ReactNode } from "react";
import { apiRequest } from "@/lib/api";
import type { ValidationProfile, AppRole } from "@/lib/types";

// ---------------------------------------------------------------------------
// Local types
// ---------------------------------------------------------------------------

type AudiencePref = "analyst" | "business" | "external";

/** UI-friendly clone-form state — spreads converted to % for human editing. */
interface CloneFormValues {
  name: string;
  independence_level: "method" | "method_x_source_class";
  high_min_distinct_methods: number;
  /** Stored in DB as 0–1 decimal; displayed here as 0–100 %. */
  high_max_spread_pct: number;
  high_require_tier_a: boolean;
  high_min_source_classes: number;
  medium_min_distinct_methods: number;
  medium_max_spread_pct: number;
  use_alt_medium: boolean;
  medium_alt_min_methods: number;
  medium_alt_max_spread_pct: number;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface SettingsClientProps {
  initialProfiles: ValidationProfile[];
  initialWebSearch: boolean;
  initialAudience: string;
  canManage: boolean;
  userRole: AppRole | null;
}

// ---------------------------------------------------------------------------
// Tiny shared presentational helpers
// ---------------------------------------------------------------------------

function pctStr(decimal: number): string {
  return `${(decimal * 100).toFixed(1)} %`;
}

function SectionCard({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <section className={`card p-6 ${className}`}>{children}</section>;
}

function SectionHead({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string;
  title: string;
  description: ReactNode;
}) {
  return (
    <div className="mb-5 border-b border-line pb-4">
      <div className="eyebrow mb-0.5">{eyebrow}</div>
      <h2 className="text-base font-semibold text-ink">{title}</h2>
      <p className="mt-1 max-w-2xl text-sm text-ink-muted">{description}</p>
    </div>
  );
}

function InlineMsg({
  type,
  text,
}: {
  type: "success" | "error";
  text: string;
}) {
  return (
    <p
      role="status"
      className={`mt-3 text-xs ${
        type === "error" ? "text-red-600" : "text-confidence-high"
      }`}
    >
      {type === "error" ? "Error: " : ""}
      {text}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Profile card
// ---------------------------------------------------------------------------

function ProfileCard({
  profile,
  activating,
  canManage,
  onActivate,
  onClone,
}: {
  profile: ValidationProfile;
  activating: boolean;
  canManage: boolean;
  onActivate: (id: number) => void;
  onClone: (p: ValidationProfile) => void;
}) {
  const active = profile.is_active;
  return (
    <div
      className={`relative flex flex-col rounded-card border p-4 transition-shadow ${
        active
          ? "border-brand bg-brand-50 shadow-raised"
          : "border-line bg-surface hover:shadow-card"
      }`}
    >
      {/* Header */}
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-1.5">
            <h3 className="text-sm font-semibold text-ink">{profile.name}</h3>
            {active && (
              <span className="badge bg-brand text-white" style={{ fontSize: "0.6rem" }}>
                Active
              </span>
            )}
          </div>
          <div className="mt-0.5 text-2xs text-ink-subtle">
            {profile.independence_level === "method_x_source_class"
              ? "method × source class"
              : "method only"}
          </div>
        </div>
      </div>

      {/* HIGH band */}
      <div className="mb-2 flex-1 rounded-md bg-confidence-high-bg px-3 py-2">
        <div className="mb-1.5 text-2xs font-semibold uppercase tracking-widest text-confidence-high">
          HIGH
        </div>
        <Row label="Methods" value={`≥ ${profile.high_min_distinct_methods}`} />
        <Row label="Spread" value={`< ${pctStr(profile.high_max_spread)}`} />
        <Row
          label="Source classes"
          value={`≥ ${profile.high_min_source_classes}`}
        />
        {profile.high_require_tier_a && (
          <div className="mt-1 text-2xs text-ink-muted">Tier A required</div>
        )}
      </div>

      {/* MEDIUM band */}
      <div className="mb-3 rounded-md bg-confidence-medium-bg px-3 py-2">
        <div className="mb-1.5 text-2xs font-semibold uppercase tracking-widest text-confidence-medium">
          MEDIUM
        </div>
        <Row
          label="Methods"
          value={`≥ ${profile.medium_min_distinct_methods}`}
        />
        <Row
          label="Spread"
          value={`< ${pctStr(profile.medium_max_spread)}`}
        />
        {profile.medium_alt_min_methods != null && (
          <div className="mt-1 text-2xs text-ink-muted">
            Alt: ≥ {profile.medium_alt_min_methods} methods,{" "}
            spread &lt; {pctStr(profile.medium_alt_max_spread ?? 0)}
          </div>
        )}
      </div>

      {/* Actions */}
      {canManage && (
        <div className="flex gap-2">
          <button
            onClick={() => onActivate(profile.profile_id)}
            disabled={active || activating}
            className={`focusable flex-1 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              active
                ? "cursor-default bg-brand text-white opacity-80"
                : "bg-surface-subtle text-ink hover:bg-line disabled:opacity-40"
            }`}
          >
            {active ? "Active" : "Activate"}
          </button>
          <button
            onClick={() => onClone(profile)}
            title="Clone & customize this profile"
            className="focusable rounded-md border border-line px-2.5 py-1.5 text-xs font-medium text-ink-muted hover:border-brand/40 hover:text-ink"
          >
            Clone
          </button>
        </div>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-2xs text-ink-muted">{label}</span>
      <span className="font-mono tnum text-2xs text-ink">{value}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Clone & tweak form
// ---------------------------------------------------------------------------

function initCloneForm(base: ValidationProfile): CloneFormValues {
  return {
    name: `${base.name} (custom)`,
    independence_level: base.independence_level,
    high_min_distinct_methods: base.high_min_distinct_methods,
    high_max_spread_pct: parseFloat((base.high_max_spread * 100).toFixed(1)),
    high_require_tier_a: base.high_require_tier_a,
    high_min_source_classes: base.high_min_source_classes,
    medium_min_distinct_methods: base.medium_min_distinct_methods,
    medium_max_spread_pct: parseFloat(
      (base.medium_max_spread * 100).toFixed(1),
    ),
    use_alt_medium: base.medium_alt_min_methods != null,
    medium_alt_min_methods: base.medium_alt_min_methods ?? 3,
    medium_alt_max_spread_pct: parseFloat(
      ((base.medium_alt_max_spread ?? 0.2) * 100).toFixed(1),
    ),
  };
}

function NumberKnob({
  label,
  hint,
  value,
  min,
  max,
  step = 1,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (v: number) => void;
}) {
  return (
    <div>
      <label className="mb-1 block text-xs font-medium text-ink">
        {label}
        {hint && (
          <span className="ml-1 font-normal text-ink-subtle">{hint}</span>
        )}
      </label>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => {
          const parsed = parseFloat(e.target.value);
          if (!Number.isNaN(parsed)) onChange(parsed);
        }}
        className="w-full rounded-md border border-line bg-surface px-2.5 py-1.5 text-sm font-mono tnum text-ink focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
      />
    </div>
  );
}

function CloneForm({
  base,
  pending,
  onSubmit,
  onCancel,
}: {
  base: ValidationProfile;
  pending: boolean;
  onSubmit: (form: CloneFormValues) => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState<CloneFormValues>(() =>
    initCloneForm(base),
  );

  function set<K extends keyof CloneFormValues>(
    key: K,
    value: CloneFormValues[K],
  ) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  return (
    <div className="mt-5 rounded-card border border-brand/25 bg-brand-50 p-5">
      <h3 className="mb-4 text-sm font-semibold text-ink">
        Clone &amp; customize —{" "}
        <span className="font-normal text-ink-muted">based on {base.name}</span>
      </h3>

      {/* Profile name */}
      <div className="mb-5">
        <label className="mb-1 block text-xs font-medium text-ink">
          Profile name
        </label>
        <input
          type="text"
          value={form.name}
          onChange={(e) => set("name", e.target.value)}
          placeholder="e.g. Standard + dual source class"
          className="w-full rounded-md border border-line bg-surface px-2.5 py-1.5 text-sm text-ink focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
        />
      </div>

      {/* Independence level */}
      <div className="mb-5">
        <div className="mb-1.5 text-xs font-medium text-ink">
          Independence counting
        </div>
        <div className="flex flex-wrap gap-4">
          {(
            [
              ["method", "Method only"],
              ["method_x_source_class", "Method × source class (recommended)"],
            ] as const
          ).map(([val, label]) => (
            <label
              key={val}
              className="flex cursor-pointer items-center gap-1.5 text-xs text-ink"
            >
              <input
                type="radio"
                name="independence_level"
                value={val}
                checked={form.independence_level === val}
                onChange={() => set("independence_level", val)}
                className="accent-brand"
              />
              {label}
            </label>
          ))}
        </div>
      </div>

      {/* HIGH thresholds */}
      <div className="mb-5">
        <div className="mb-2 text-2xs font-semibold uppercase tracking-widest text-confidence-high">
          HIGH confidence thresholds
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <NumberKnob
            label="Min methods"
            value={form.high_min_distinct_methods}
            min={1}
            max={10}
            onChange={(v) => set("high_min_distinct_methods", v)}
          />
          <NumberKnob
            label="Max spread"
            hint="(%)"
            value={form.high_max_spread_pct}
            min={0.1}
            max={100}
            step={0.1}
            onChange={(v) => set("high_max_spread_pct", v)}
          />
          <NumberKnob
            label="Min source classes"
            value={form.high_min_source_classes}
            min={1}
            max={4}
            onChange={(v) => set("high_min_source_classes", v)}
          />
          <div>
            <div className="mb-1 text-xs font-medium text-ink">
              Require Tier A
            </div>
            <label className="flex cursor-pointer items-center gap-2 pt-1.5 text-xs text-ink">
              <input
                type="checkbox"
                checked={form.high_require_tier_a}
                onChange={(e) => set("high_require_tier_a", e.target.checked)}
                className="h-4 w-4 accent-brand"
              />
              Required
            </label>
          </div>
        </div>
      </div>

      {/* MEDIUM thresholds */}
      <div className="mb-5">
        <div className="mb-2 text-2xs font-semibold uppercase tracking-widest text-confidence-medium">
          MEDIUM confidence thresholds
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <NumberKnob
            label="Min methods"
            value={form.medium_min_distinct_methods}
            min={1}
            max={10}
            onChange={(v) => set("medium_min_distinct_methods", v)}
          />
          <NumberKnob
            label="Max spread"
            hint="(%)"
            value={form.medium_max_spread_pct}
            min={0.1}
            max={100}
            step={0.1}
            onChange={(v) => set("medium_max_spread_pct", v)}
          />
        </div>

        {/* Alternative MEDIUM path */}
        <div className="mt-3">
          <label className="mb-2 flex cursor-pointer items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={form.use_alt_medium}
              onChange={(e) => set("use_alt_medium", e.target.checked)}
              className="h-4 w-4 accent-brand"
            />
            <span className="font-medium text-ink">
              Add alternative MEDIUM path
            </span>
            <span className="text-ink-subtle">
              — a cell qualifying either path earns MEDIUM
            </span>
          </label>
          {form.use_alt_medium && (
            <div className="grid grid-cols-2 gap-3 pl-6 sm:grid-cols-4">
              <NumberKnob
                label="Alt min methods"
                value={form.medium_alt_min_methods}
                min={1}
                max={10}
                onChange={(v) => set("medium_alt_min_methods", v)}
              />
              <NumberKnob
                label="Alt max spread"
                hint="(%)"
                value={form.medium_alt_max_spread_pct}
                min={0.1}
                max={100}
                step={0.1}
                onChange={(v) => set("medium_alt_max_spread_pct", v)}
              />
            </div>
          )}
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-3">
        <button
          onClick={() => onSubmit(form)}
          disabled={pending || !form.name.trim()}
          className="focusable rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50"
        >
          {pending ? "Creating…" : "Create profile"}
        </button>
        <button
          onClick={onCancel}
          className="focusable rounded-lg border border-line px-4 py-2 text-sm font-medium text-ink hover:bg-surface-subtle"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Toggle switch
// ---------------------------------------------------------------------------

function Toggle({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => !disabled && onChange(!checked)}
      className={`focusable relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
        checked ? "bg-brand" : "bg-line"
      } ${disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"}`}
    >
      <span
        className={`inline-block h-4 w-4 rounded-full bg-white shadow transition-transform ${
          checked ? "translate-x-6" : "translate-x-1"
        }`}
      />
    </button>
  );
}

// ---------------------------------------------------------------------------
// Audience options
// ---------------------------------------------------------------------------

const AUDIENCE_OPTIONS: {
  value: AudiencePref;
  label: string;
  description: string;
}[] = [
  {
    value: "analyst",
    label: "Analyst",
    description:
      "Full drill chain, raw payloads, assumption ledger, unfiltered commentary.",
  },
  {
    value: "business",
    label: "Business",
    description:
      "TAM bands, player shares, executive commentary. No raw drill.",
  },
  {
    value: "external",
    label: "External",
    description:
      "Published outputs only — no raw drill, no assumptions, no admin surfaces.",
  },
];

// ---------------------------------------------------------------------------
// Main exported component
// ---------------------------------------------------------------------------

export function SettingsClient({
  initialProfiles,
  initialWebSearch,
  initialAudience,
  canManage,
  userRole,
}: SettingsClientProps) {
  // -- Profile state ---------------------------------------------------------
  const [profiles, setProfiles] = useState(initialProfiles);
  const [cloneBase, setCloneBase] = useState<ValidationProfile | null>(null);
  const [activatingId, setActivatingId] = useState<number | null>(null);
  const [creatingProfile, setCreatingProfile] = useState(false);
  const [profileMsg, setProfileMsg] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);

  // -- Web-search state ------------------------------------------------------
  const [webSearch, setWebSearch] = useState(initialWebSearch);
  const [togglingWS, setTogglingWS] = useState(false);
  const [wsMsg, setWsMsg] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);

  // -- Audience state --------------------------------------------------------
  const [audience, setAudience] = useState(initialAudience);
  const [switchingAudience, setSwitchingAudience] = useState(false);
  const [audienceMsg, setAudienceMsg] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);

  // -- Handlers --------------------------------------------------------------

  async function handleActivate(profileId: number) {
    const prev = profiles;
    // Optimistic update
    setProfiles((ps) =>
      ps.map((p) => ({ ...p, is_active: p.profile_id === profileId })),
    );
    setActivatingId(profileId);
    setProfileMsg(null);
    try {
      await apiRequest(`/settings/profiles/${profileId}/activate`, {
        method: "PUT",
      });
      setProfileMsg({
        type: "success",
        text: "Profile activated. Cell confidence scores will re-derive on the next page refresh (summary view re-queries automatically).",
      });
    } catch (err: unknown) {
      setProfiles(prev);
      setProfileMsg({
        type: "error",
        text: err instanceof Error ? err.message : "Failed to activate profile.",
      });
    } finally {
      setActivatingId(null);
    }
  }

  async function handleCloneSubmit(form: CloneFormValues) {
    setCreatingProfile(true);
    setProfileMsg(null);
    try {
      // Backend: POST /settings/profiles/clone with ValidationProfileCloneIn body.
      // source_profile_id is required; per-threshold overrides are optional.
      const body = {
        source_profile_id: cloneBase!.profile_id,
        name: form.name.trim(),
        independence_level: form.independence_level,
        high_min_distinct_methods: form.high_min_distinct_methods,
        high_max_spread: form.high_max_spread_pct / 100,
        high_require_tier_a: form.high_require_tier_a,
        high_min_source_classes: form.high_min_source_classes,
        medium_min_distinct_methods: form.medium_min_distinct_methods,
        medium_max_spread: form.medium_max_spread_pct / 100,
        medium_alt_min_methods: form.use_alt_medium
          ? form.medium_alt_min_methods
          : null,
        medium_alt_max_spread: form.use_alt_medium
          ? form.medium_alt_max_spread_pct / 100
          : null,
      };
      const created = await apiRequest<ValidationProfile>(
        "/settings/profiles/clone",
        { method: "POST", json: body },
      );
      setProfiles((ps) => [...ps, created]);
      setCloneBase(null);
      setProfileMsg({
        type: "success",
        text: `Profile "${created.name}" created. Activate it to make it the confidence engine baseline.`,
      });
    } catch (err: unknown) {
      setProfileMsg({
        type: "error",
        text:
          err instanceof Error ? err.message : "Failed to create profile.",
      });
    } finally {
      setCreatingProfile(false);
    }
  }

  async function handleWebSearchToggle(enabled: boolean) {
    const prev = webSearch;
    setWebSearch(enabled);
    setTogglingWS(true);
    setWsMsg(null);
    try {
      await apiRequest("/settings/web-search", {
        method: "PUT",
        json: { enabled },
      });
      setWsMsg({
        type: "success",
        text: `Web-search fallback ${enabled ? "enabled" : "disabled"} for this engagement.`,
      });
    } catch (err: unknown) {
      setWebSearch(prev);
      setWsMsg({
        type: "error",
        text:
          err instanceof Error ? err.message : "Failed to update setting.",
      });
    } finally {
      setTogglingWS(false);
    }
  }

  async function handleAudienceChange(aud: string) {
    const prev = audience;
    setAudience(aud);
    setSwitchingAudience(true);
    setAudienceMsg(null);
    try {
      await apiRequest("/settings/audience", {
        method: "PUT",
        json: { audience: aud },
      });
      setAudienceMsg({
        type: "success",
        text: `Audience view switched to "${aud}". Commentary and drill visibility now reflect that audience.`,
      });
    } catch (err: unknown) {
      setAudience(prev);
      setAudienceMsg({
        type: "error",
        text:
          err instanceof Error ? err.message : "Failed to update preference.",
      });
    } finally {
      setSwitchingAudience(false);
    }
  }

  // Audience switching is allowed for analyst and above.
  const canSwitchAudience =
    userRole === "owner_admin" || userRole === "analyst";

  // ---- Render --------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* ================================================================
          1. Validation Profile
         ================================================================ */}
      <SectionCard>
        <SectionHead
          eyebrow="Confidence Engine"
          title="Validation Profile"
          description={
            <>
              Controls how many independent method–source signals must agree
              before a cell earns{" "}
              <strong className="font-semibold text-confidence-high">
                HIGH
              </strong>{" "}
              or{" "}
              <strong className="font-semibold text-confidence-medium">
                MEDIUM
              </strong>{" "}
              confidence. Confidence is computed by the{" "}
              <code className="rounded bg-surface-subtle px-1 text-2xs font-mono">
                cell_triangulation_summary
              </code>{" "}
              view — it is never set manually. Changing the active profile
              immediately re-derives every cell&apos;s confidence on the next
              query.{" "}
              {!canManage && (
                <span className="text-ink-subtle">
                  Owner / Admin role required to change the active profile.
                </span>
              )}
            </>
          }
        />

        {profiles.length === 0 ? (
          <div className="rounded-md border border-line bg-surface-subtle px-4 py-8 text-center">
            <p className="text-sm text-ink-muted">
              No validation profiles loaded.
            </p>
            <p className="mt-1 text-xs text-ink-subtle">
              Ensure the backend is running and the Liquibase seed (changeset
              grx10:1010) has been applied.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {profiles.map((p) => (
              <ProfileCard
                key={p.profile_id}
                profile={p}
                activating={activatingId === p.profile_id}
                canManage={canManage}
                onActivate={handleActivate}
                onClone={(prof) => {
                  setCloneBase(prof);
                  setProfileMsg(null);
                }}
              />
            ))}
          </div>
        )}

        {profileMsg && (
          <InlineMsg type={profileMsg.type} text={profileMsg.text} />
        )}

        {canManage && cloneBase && (
          <CloneForm
            base={cloneBase}
            pending={creatingProfile}
            onSubmit={handleCloneSubmit}
            onCancel={() => setCloneBase(null)}
          />
        )}
      </SectionCard>

      {/* ================================================================
          2. Web-Search Fallback (Q8)
         ================================================================ */}
      <SectionCard>
        <SectionHead
          eyebrow="Pipeline Fallback"
          title="Web-Search Fallback"
          description={
            <>
              When enabled, the pipeline issues a web search for any cell that
              has insufficient structured estimates. Extracted values are always{" "}
              <strong className="font-semibold">Class C</strong>, confidence
              hard-capped at{" "}
              <strong className="font-semibold text-confidence-low">LOW</strong>
              , and tagged{" "}
              <code className="rounded bg-surface-subtle px-1 text-2xs font-mono">
                method_code = web_search_extraction
              </code>
              . The discovery URL is auto-registered as a source; the extraction
              snippet is stored verbatim as the raw payload — no fabricated data.
              Enabled by default; disable if all cells must rely entirely on
              structured connectors.{" "}
              {!canManage && (
                <span className="text-ink-subtle">
                  Owner / Admin role required to change this setting.
                </span>
              )}
            </>
          }
        />

        <div className="flex items-center gap-3">
          <Toggle
            checked={webSearch}
            disabled={!canManage || togglingWS}
            label="Web-search fallback enabled"
            onChange={handleWebSearchToggle}
          />
          <div>
            <span className="text-sm font-medium text-ink">
              {webSearch ? "Enabled" : "Disabled"}
            </span>
            <span className="ml-2 text-sm text-ink-muted">
              {webSearch
                ? "— fills coverage gaps; estimates capped at LOW confidence"
                : "— cells rely entirely on structured connectors"}
            </span>
          </div>
        </div>

        {wsMsg && <InlineMsg type={wsMsg.type} text={wsMsg.text} />}
      </SectionCard>

      {/* ================================================================
          3. Audience Switcher
         ================================================================ */}
      <SectionCard>
        <SectionHead
          eyebrow="Content Filtering"
          title="Audience View"
          description="Preview the platform through a specific audience lens. Commentary, report framing, and drill-chain visibility adjust to match the selected audience. This is a session preference — it does not change what other users see."
        />

        {canSwitchAudience ? (
          <div className="flex flex-col gap-3 sm:flex-row">
            {AUDIENCE_OPTIONS.map((opt) => {
              const active = audience === opt.value;
              return (
                <button
                  key={opt.value}
                  onClick={() => handleAudienceChange(opt.value)}
                  disabled={switchingAudience}
                  className={`focusable flex-1 rounded-card border p-4 text-left transition-shadow ${
                    active
                      ? "border-brand bg-brand-50 shadow-raised"
                      : "border-line bg-surface hover:border-brand/40 hover:shadow-card"
                  } disabled:opacity-60`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-semibold text-ink">
                      {opt.label}
                    </span>
                    {active && (
                      <span
                        className="badge bg-brand text-white"
                        style={{ fontSize: "0.6rem" }}
                      >
                        Active
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-xs text-ink-muted">
                    {opt.description}
                  </p>
                </button>
              );
            })}
          </div>
        ) : (
          <div className="rounded-md border border-line bg-surface-subtle px-4 py-4">
            <p className="text-sm text-ink-muted">
              Audience switching is available to Analyst and Owner / Admin roles.
            </p>
            <p className="mt-1 text-xs text-ink-subtle">
              Your current role ({userRole ?? "guest"}) sees the{" "}
              <strong>
                {audience === "all" ? "default" : audience}
              </strong>{" "}
              view.
            </p>
          </div>
        )}

        {audienceMsg && (
          <InlineMsg type={audienceMsg.type} text={audienceMsg.text} />
        )}
      </SectionCard>
    </div>
  );
}

export default SettingsClient;
