"use client";

import { useCallback, useMemo, useState, type MouseEvent } from "react";
import { ConnectorHealthBadge } from "@/components/ConnectorHealthBadge";
import { DataTable, type Column } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { api } from "@/lib/api";
import { formatTimestamp } from "@/lib/format";
import type { AppRole, Source, SourceClass } from "@/lib/types";
import { useApi } from "@/lib/swr";
import { ConnectorDetailDrawer } from "./ConnectorDetailDrawer";
import { AddCustomRestModal } from "./AddCustomRestModal";
import { AddScrapingModal } from "./AddScrapingModal";

// ---------------------------------------------------------------------------
// Class metadata (label, description, colour)
// ---------------------------------------------------------------------------

const CLASS_META: Record<
  SourceClass,
  { label: string; description: string; cls: string; dotCls: string }
> = {
  A: {
    label: "A",
    description: "Primary structured",
    cls: "bg-emerald-50 text-emerald-700 border border-emerald-200",
    dotCls: "bg-emerald-500",
  },
  B: {
    label: "B",
    description: "Industry / procedural",
    cls: "bg-amber-50 text-amber-700 border border-amber-200",
    dotCls: "bg-amber-500",
  },
  C: {
    label: "C",
    description: "Triangulation support",
    cls: "bg-slate-100 text-slate-600 border border-slate-200",
    dotCls: "bg-slate-400",
  },
};

const ACCESS_META: Record<string, { label: string; cls: string }> = {
  api: { label: "API", cls: "bg-brand-50 text-brand-700" },
  scrape: { label: "Scrape ⚠", cls: "bg-orange-50 text-orange-700" },
  web_search: { label: "Web search", cls: "bg-blue-50 text-blue-700" },
  manual_upload: { label: "Manual", cls: "bg-slate-100 text-slate-600" },
};

// ---------------------------------------------------------------------------
// Small inline badge components
// ---------------------------------------------------------------------------

function ClassBadge({ cls }: { cls: SourceClass | null }) {
  if (!cls) return <span className="text-ink-subtle text-xs">—</span>;
  const m = CLASS_META[cls];
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ${m.cls}`}
      title={m.description}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${m.dotCls}`} aria-hidden />
      {m.label}
    </span>
  );
}

function AccessBadge({ method }: { method: string | null }) {
  if (!method) return <span className="text-ink-subtle text-xs">—</span>;
  const m = ACCESS_META[method] ?? { label: method, cls: "bg-slate-100 text-slate-600" };
  return (
    <span className={`badge text-2xs ${m.cls}`}>{m.label}</span>
  );
}

// ---------------------------------------------------------------------------
// Status buckets — mutually exclusive and exhaustive, so counts always sum
// to the visible total and each count doubles as a filter.
// ---------------------------------------------------------------------------

type StatusBucket = "ok" | "failing" | "budget" | "disabled" | "unprobed";
type StatusFilter = "all" | StatusBucket;

/** Assign every source to exactly ONE bucket (precedence top-down). */
function bucketOf(s: Source): StatusBucket {
  if (!s.enabled) return "disabled";
  if (
    s.last_probe_status &&
    s.last_probe_status !== "OK" &&
    s.last_probe_status !== "EMPTY"
  )
    return "failing";
  if (s.budget_warning) return "budget";
  if (s.last_probe_status === "OK") return "ok";
  return "unprobed"; // never probed, or last probe returned EMPTY
}

const STATUS_META: Record<
  StatusBucket,
  { label: string; dot: string; activeCls: string; countCls: string; title: string }
> = {
  ok: {
    label: "OK",
    dot: "bg-emerald-500",
    activeCls: "border-emerald-300 bg-emerald-50",
    countCls: "text-emerald-700",
    title: "Enabled and last probe succeeded",
  },
  failing: {
    label: "Failing",
    dot: "bg-red-500",
    activeCls: "border-red-300 bg-red-50",
    countCls: "text-red-700",
    title: "Enabled but last probe failed (auth / quota / unreachable / schema)",
  },
  budget: {
    label: "Budget warning",
    dot: "bg-orange-500",
    activeCls: "border-orange-300 bg-orange-50",
    countCls: "text-orange-700",
    title: "Approaching its monthly budget or quota ceiling (~80%)",
  },
  disabled: {
    label: "Disabled",
    dot: "bg-slate-300",
    activeCls: "border-slate-300 bg-slate-100",
    countCls: "text-slate-600",
    title: "In the catalog but switched off — not pulled by the pipeline",
  },
  unprobed: {
    label: "Not probed yet",
    dot: "bg-sky-400",
    activeCls: "border-sky-300 bg-sky-50",
    countCls: "text-sky-700",
    title: "Enabled but health has never been checked (or last probe returned no data) — click Probe on a row",
  },
};

const STATUS_ORDER: StatusBucket[] = ["ok", "failing", "budget", "unprobed", "disabled"];

function StatusChips({
  sources,
  active,
  onChange,
}: {
  sources: Source[];
  active: StatusFilter;
  onChange: (f: StatusFilter) => void;
}) {
  const counts = useMemo(() => {
    const c: Record<StatusBucket, number> = {
      ok: 0, failing: 0, budget: 0, disabled: 0, unprobed: 0,
    };
    for (const s of sources) c[bucketOf(s)] += 1;
    return c;
  }, [sources]);

  return (
    <div className="flex flex-wrap items-center gap-2 text-sm" role="group" aria-label="Filter by health status">
      <button
        onClick={() => onChange("all")}
        className={`focusable flex items-center gap-1.5 rounded-lg border px-2.5 py-1 transition-colors ${
          active === "all"
            ? "border-brand/40 bg-brand/5 font-medium text-ink"
            : "border-line bg-white text-ink-muted hover:bg-surface-subtle"
        }`}
        title="Show all connectors"
      >
        Total
        <span className="font-semibold tabular-nums text-ink">{sources.length}</span>
      </button>
      {STATUS_ORDER.map((b) => (
        <button
          key={b}
          onClick={() => onChange(active === b ? "all" : b)}
          className={`focusable flex items-center gap-1.5 rounded-lg border px-2.5 py-1 transition-colors ${
            active === b
              ? `${STATUS_META[b].activeCls} font-medium text-ink`
              : "border-line bg-white text-ink-muted hover:bg-surface-subtle"
          }`}
          title={STATUS_META[b].title}
          aria-pressed={active === b}
        >
          <span className={`h-2 w-2 rounded-full ${STATUS_META[b].dot}`} aria-hidden />
          {STATUS_META[b].label}
          <span className={`font-semibold tabular-nums ${STATUS_META[b].countCls}`}>
            {counts[b]}
          </span>
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Always-visible class legend (what A / B / C actually mean)
// ---------------------------------------------------------------------------

function ClassLegend() {
  return (
    <div className="mb-4 grid gap-2 rounded-xl border border-line bg-surface-subtle px-4 py-3 text-xs leading-relaxed text-ink-muted sm:grid-cols-3">
      <div>
        <span className="inline-flex items-center gap-1 font-semibold text-emerald-700">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden />
          Class A — Primary structured.
        </span>{" "}
        Authoritative programmatic sources (government APIs, official filings).
        Can qualify a cell for <strong>HIGH</strong> confidence.
      </div>
      <div>
        <span className="inline-flex items-center gap-1 font-semibold text-amber-700">
          <span className="h-1.5 w-1.5 rounded-full bg-amber-500" aria-hidden />
          Class B — Industry / procedural.
        </span>{" "}
        Industry reports, associations, earnings calls. Caps a cell at{" "}
        <strong>MEDIUM</strong> confidence.
      </div>
      <div>
        <span className="inline-flex items-center gap-1 font-semibold text-slate-600">
          <span className="h-1.5 w-1.5 rounded-full bg-slate-400" aria-hidden />
          Class C — Triangulation support.
        </span>{" "}
        Macro stats, news, patents, web search. Gap-fill only — hard-capped at{" "}
        <strong>LOW</strong> confidence.
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Class filter tab bar
// ---------------------------------------------------------------------------

type ClassFilter = "all" | SourceClass;

const FILTER_TABS: { key: ClassFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "A", label: "A — Primary structured" },
  { key: "B", label: "B — Industry" },
  { key: "C", label: "C — Support" },
];

interface FilterTabsProps {
  active: ClassFilter;
  counts: Record<ClassFilter, number>;
  onChange: (f: ClassFilter) => void;
}

function FilterTabs({ active, counts, onChange }: FilterTabsProps) {
  return (
    <div
      role="tablist"
      aria-label="Filter by connector class"
      className="flex flex-wrap gap-1"
    >
      {FILTER_TABS.map((tab) => (
        <button
          key={tab.key}
          role="tab"
          aria-selected={active === tab.key}
          onClick={() => onChange(tab.key)}
          className={`focusable rounded-lg px-3 py-1.5 text-sm transition-colors ${
            active === tab.key
              ? "bg-brand text-white font-medium shadow-sm"
              : "text-ink-muted hover:bg-surface-subtle hover:text-ink"
          }`}
        >
          {tab.label}
          <span
            className={`ml-1.5 rounded-full px-1.5 py-0.5 text-2xs tabular-nums ${
              active === tab.key
                ? "bg-white/20 text-white"
                : "bg-surface-subtle text-ink-subtle"
            }`}
          >
            {counts[tab.key]}
          </span>
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main client component
// ---------------------------------------------------------------------------

export interface ConnectorsClientProps {
  initialSources: Source[];
  canEnterCredentials: boolean;
  role: AppRole;
}

export function ConnectorsClient({
  initialSources,
  canEnterCredentials,
  role: _role,
}: ConnectorsClientProps) {
  // ── SWR for live source list (probe/add mutations update this) ────────────
  const {
    data: sources = initialSources,
    mutate,
  } = useApi<Source[]>("/connectors", undefined, {
    fallbackData: initialSources,
    revalidateOnFocus: false,
    dedupingInterval: 10_000,
  });

  // ── UI state ──────────────────────────────────────────────────────────────
  const [classFilter, setClassFilter] = useState<ClassFilter>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showAddRest, setShowAddRest] = useState(false);
  const [showAddScraping, setShowAddScraping] = useState(false);

  // ── Derived data ──────────────────────────────────────────────────────────

  // Class + search narrow the working set; the status chips count THIS set
  // (so the bucket counts always sum to the visible Total)…
  const scopedSources = useMemo(() => {
    let list = classFilter === "all" ? sources : sources.filter((s) => s.class === classFilter);
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      list = list.filter(
        (s) =>
          s.source_id.toLowerCase().includes(q) ||
          s.publisher.toLowerCase().includes(q) ||
          (s.raw_table ?? "").toLowerCase().includes(q) ||
          (s.connector ?? "").toLowerCase().includes(q),
      );
    }
    return list;
  }, [sources, classFilter, search]);

  // …and clicking a status chip additionally filters the table rows.
  const filteredSources = useMemo(
    () =>
      statusFilter === "all"
        ? scopedSources
        : scopedSources.filter((s) => bucketOf(s) === statusFilter),
    [scopedSources, statusFilter],
  );

  const classCounts = useMemo((): Record<ClassFilter, number> => {
    const counts = (key: ClassFilter) =>
      key === "all" ? sources.length : sources.filter((s) => s.class === key).length;
    return { all: counts("all"), A: counts("A"), B: counts("B"), C: counts("C") };
  }, [sources]);

  const selectedSource = useMemo(
    () => (selectedId ? (sources.find((s) => s.source_id === selectedId) ?? null) : null),
    [sources, selectedId],
  );

  // ── Mutation helpers ──────────────────────────────────────────────────────

  const handleSourceUpdated = useCallback(
    (updated: Source) => {
      mutate(
        (prev) => prev?.map((s) => (s.source_id === updated.source_id ? updated : s)),
        { revalidate: false },
      );
    },
    [mutate],
  );

  const handleSourceAdded = useCallback(
    (added: Source) => {
      mutate((prev) => [added, ...(prev ?? [])], { revalidate: false });
      setShowAddRest(false);
      setShowAddScraping(false);
      setSelectedId(added.source_id);
    },
    [mutate],
  );

  // ── Inline probe from the table action button ─────────────────────────────
  const [probingId, setProbingId] = useState<string | null>(null);

  async function handleInlineProbe(sourceId: string, e: MouseEvent) {
    e.stopPropagation(); // Don't open the drawer
    setProbingId(sourceId);
    try {
      const updated = await api.probeSource(sourceId);
      handleSourceUpdated(updated);
    } finally {
      setProbingId(null);
    }
  }

  // ── DataTable columns ─────────────────────────────────────────────────────

  const columns: Column<Source>[] = [
    {
      key: "source",
      header: "Source",
      sortValue: (s) => s.publisher,
      render: (s) => (
        <div className="min-w-0">
          <div className="font-medium text-ink truncate">{s.publisher}</div>
          <div className="font-mono text-2xs text-ink-subtle truncate">
            {s.source_id}
          </div>
        </div>
      ),
    },
    {
      key: "class",
      header: "Class",
      width: "6rem",
      align: "center",
      sortValue: (s) => s.class ?? "Z",
      render: (s) => <ClassBadge cls={s.class} />,
    },
    {
      key: "access",
      header: "Access",
      width: "8rem",
      sortValue: (s) => s.access_method ?? "",
      render: (s) => <AccessBadge method={s.access_method} />,
    },
    {
      key: "raw_table",
      header: "Raw table",
      width: "13rem",
      sortValue: (s) => s.raw_table ?? "",
      render: (s) =>
        s.raw_table ? (
          <span className="font-mono text-xs text-ink-muted">{s.raw_table}</span>
        ) : (
          <span className="text-ink-subtle text-xs">—</span>
        ),
    },
    {
      key: "health",
      header: "Health",
      width: "11rem",
      sortValue: (s) => s.last_probe_status ?? "ZZZZZ",
      render: (s) => (
        <ConnectorHealthBadge
          status={s.last_probe_status}
          budgetWarning={s.budget_warning}
          detail={s.last_probe_detail ?? undefined}
          size="sm"
        />
      ),
    },
    {
      key: "last_probed",
      header: "Last probed",
      width: "9rem",
      sortValue: (s) => s.last_probe_at ?? "",
      render: (s) => (
        <span className="text-xs text-ink-subtle">
          {s.last_probe_at ? formatTimestamp(s.last_probe_at) : "Never"}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      width: canEnterCredentials ? "10rem" : "7rem",
      render: (s) => (
        <div
          className="flex items-center gap-1.5"
          onClick={(e) => e.stopPropagation()}
        >
          <button
            onClick={(e) => handleInlineProbe(s.source_id, e)}
            disabled={probingId === s.source_id}
            title="Run a cheap probe to refresh connector health"
            className="focusable rounded-md px-2 py-1 text-xs text-ink-muted hover:bg-surface-subtle hover:text-ink disabled:opacity-50 transition-colors"
          >
            {probingId === s.source_id ? "…" : "Probe"}
          </button>
          {canEnterCredentials && s.auth && s.auth !== "none" && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                setSelectedId(s.source_id);
              }}
              title="Manage credential"
              className="focusable rounded-md px-2 py-1 text-xs text-ink-muted hover:bg-surface-subtle hover:text-ink transition-colors"
            >
              <svg
                viewBox="0 0 24 24"
                className="h-3.5 w-3.5"
                fill="none"
                stroke="currentColor"
                strokeWidth={1.8}
                aria-hidden
              >
                <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}
          {!s.enabled && (
            <span className="text-2xs text-slate-400 font-medium">off</span>
          )}
        </div>
      ),
    },
  ];

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <>
      <PageHeader
        eyebrow="Track B"
        title="Connectors"
        description="Catalog of data sources grouped by confidence class. Select a connector to view health details, manage credentials, or probe its endpoint."
        actions={
          <div className="flex gap-2">
            <button
              onClick={() => setShowAddRest(true)}
              className="btn-secondary text-sm px-3 py-2 flex items-center gap-1.5"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden>
                <path d="M12 5v14M5 12h14" strokeLinecap="round" />
              </svg>
              Add custom REST
            </button>
            <button
              onClick={() => setShowAddScraping(true)}
              className="flex items-center gap-1.5 rounded-lg border border-orange-300 bg-orange-50 px-3 py-2 text-sm font-medium text-orange-700 hover:bg-orange-100 transition-colors"
              title="Scraping sources are fragile — read the warnings before adding"
            >
              <span aria-hidden className="text-base leading-none">⚠</span>
              Add scraping source
            </button>
          </div>
        }
      />

      {/* Filter + search bar */}
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <FilterTabs
          active={classFilter}
          counts={classCounts}
          onChange={setClassFilter}
        />
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by ID, publisher, or table…"
          className="w-full sm:w-64 rounded-lg border border-line bg-white px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand/40"
          aria-label="Search connectors"
        />
      </div>

      {/* Status counts — clickable, mutually exclusive, always sum to Total */}
      <div className="mb-4">
        <StatusChips
          sources={scopedSources}
          active={statusFilter}
          onChange={setStatusFilter}
        />
      </div>

      {/* What Class A / B / C mean — always visible */}
      <ClassLegend />

      {/* Class description banner when a class filter is active */}
      {classFilter !== "all" && (
        <div
          className={`mb-4 rounded-xl border px-4 py-3 text-sm ${
            classFilter === "A"
              ? "border-emerald-200 bg-emerald-50 text-emerald-800"
              : classFilter === "B"
                ? "border-amber-200 bg-amber-50 text-amber-800"
                : "border-slate-200 bg-slate-50 text-slate-700"
          }`}
        >
          {classFilter === "A" && (
            <>
              <strong>Class A — Primary structured.</strong> Authoritative,
              programmatic sources (government APIs, official filings). Estimates
              from these sources can qualify a cell for{" "}
              <strong>HIGH confidence</strong>. HS-code versioning is the
              principal normalization trap.
            </>
          )}
          {classFilter === "B" && (
            <>
              <strong>Class B — Industry / procedural.</strong> Industry
              associations, earnings calls, filings without structured segments.
              Qualifies for <strong>MEDIUM confidence</strong>. Scraping sources
              are capped here.
            </>
          )}
          {classFilter === "C" && (
            <>
              <strong>Class C — Triangulation support.</strong> Macro stats,
              patent proxies, hiring signals, web-search extraction. Gap-fill
              and scaling only. Hard-capped at <strong>LOW confidence</strong>.
              Can seed triangulation but never manufacture HIGH or MEDIUM.
            </>
          )}
        </div>
      )}

      {/* Main catalog table */}
      <DataTable<Source>
        columns={columns}
        rows={filteredSources}
        rowKey={(s) => s.source_id}
        onRowClick={(s) => setSelectedId(s.source_id)}
        initialSort={{ key: "class", dir: "asc" }}
        empty={
          search ? (
            <span>
              No connectors match &ldquo;{search}&rdquo;.{" "}
              <button
                onClick={() => setSearch("")}
                className="text-brand hover:underline"
              >
                Clear search
              </button>
            </span>
          ) : (
            "No connectors registered yet."
          )
        }
      />

      {/* Detail drawer */}
      {selectedSource && (
        <ConnectorDetailDrawer
          source={selectedSource}
          canEnterCredentials={canEnterCredentials}
          onClose={() => setSelectedId(null)}
          onSourceUpdated={handleSourceUpdated}
        />
      )}

      {/* Add custom REST modal */}
      {showAddRest && (
        <AddCustomRestModal
          onClose={() => setShowAddRest(false)}
          onSuccess={handleSourceAdded}
        />
      )}

      {/* Add scraping modal */}
      {showAddScraping && (
        <AddScrapingModal
          onClose={() => setShowAddScraping(false)}
          onSuccess={handleSourceAdded}
        />
      )}
    </>
  );
}

export default ConnectorsClient;
