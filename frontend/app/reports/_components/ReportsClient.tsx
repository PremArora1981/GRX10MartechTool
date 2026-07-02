"use client";

/**
 * Reports screen — interactive client component.
 *
 * Three sections:
 *  1. Standard Reports  — three fixed PDFs (Executive Audit, Gap Analysis,
 *                          Player Shares). Each card has a Generate / Regenerate
 *                          button and a Download link once ready.
 *  2. Custom Builder    — cart-style section picker. User adds sections from the
 *                          left panel, reorders them in the cart on the right,
 *                          then generates a bespoke PDF.
 *  3. Excel Exports     — five flavour cards; every export includes the mandatory
 *                          _README sheet (scope, timestamp, methodology).
 *
 * All generation calls POST to the backend report/export endpoints via api.ts.
 * No data is fabricated here; every download link comes from the API response.
 */

import { useCallback, useState } from "react";
import { PageHeader } from "@/components";
import {
  api,
  ApiError,
  type ExcelFlavor,
  type ReportResult,
  type StandardReportType,
} from "@/lib/api";

// ─── Local types ─────────────────────────────────────────────────────────────

type JobStatus = "idle" | "loading" | "ready" | "error";

interface ReportJob {
  status: JobStatus;
  downloadUrl?: string;
  generatedAt?: string;
  error?: string;
}

const IDLE_JOB: ReportJob = { status: "idle" };

// ─── Static data ─────────────────────────────────────────────────────────────

interface StandardReportDef {
  type: StandardReportType;
  title: string;
  desc: string;
  note: string;
  iconPath: string;
}

const STANDARD_REPORTS: StandardReportDef[] = [
  {
    type: "executive-audit",
    title: "Executive Audit",
    desc: "Full market sizing summary: TAM by subcategory and geography, confidence distribution, active validation profile, top players, catalysts, and a numbered clickable Sources page.",
    note: "Runs against all cells in the active engagement scope.",
    iconPath:
      "M7 3h7l5 5v13H7zM14 3v5h5M9 13h6M9 17h6M3 7l2 2 4-4",
  },
  {
    type: "gap-analysis",
    title: "Gap Analysis",
    desc: "Unsized cells, cells below the confidence threshold (spread exceeds active profile limit), connectors in non-OK probe state, and prioritised recommendations to close data and source gaps.",
    note: "Thresholds read from the currently active validation profile row.",
    iconPath:
      "M3 3l7 7 4-4 7 7M21 3v6h-6M3 21v-6h6",
  },
  {
    type: "player-shares",
    title: "Player Shares",
    desc: "Top-N market participants ranked by revenue share per cell, grouped by player role (producer, distributor, OEM, buyer, CDMO). Each row carries a low–high confidence band and a traceable source citation.",
    note: "Source citations drill to the raw payload per the two-click audit chain.",
    iconPath:
      "M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75M9 7a4 4 0 100 8 4 4 0 000-8z",
  },
];

interface SectionDef {
  id: string;
  label: string;
  desc: string;
}

const AVAILABLE_SECTIONS: SectionDef[] = [
  {
    id: "executive_summary",
    label: "Executive Summary",
    desc: "Top-level TAM, confidence distribution, and key findings.",
  },
  {
    id: "market_sizing_subcategory",
    label: "Sizing by Subcategory",
    desc: "TAM table and trend charts per product category.",
  },
  {
    id: "market_sizing_geography",
    label: "Sizing by Geography",
    desc: "TAM breakdown by country and trade direction.",
  },
  {
    id: "confidence_analysis",
    label: "Confidence Analysis",
    desc: "Cell coverage, spread ratios, and effective independent-signal counts.",
  },
  {
    id: "player_shares_table",
    label: "Player Shares Table",
    desc: "Top-N participants with low–high revenue bands and source citations.",
  },
  {
    id: "player_shares_chart",
    label: "Player Shares Chart",
    desc: "Stacked-bar visualisation of market share per cell.",
  },
  {
    id: "supplier_relationships",
    label: "Supplier Relationships",
    desc: "Buyer–supplier relationship graph with evidence type and strength.",
  },
  {
    id: "assumptions_ledger",
    label: "Assumptions Ledger",
    desc: "Active assumptions with scope, unit, derivation, and version chain.",
  },
  {
    id: "triangulation_audit",
    label: "Triangulation Audit",
    desc: "Full estimate table: method code, source, and estimate per cell.",
  },
  {
    id: "catalysts",
    label: "Catalysts",
    desc: "Positive and negative events expected to shift market size.",
  },
  {
    id: "recommendations",
    label: "Recommendations",
    desc: "Prioritised strategic recommendations derived from the data.",
  },
  {
    id: "sources_reference",
    label: "Sources & References",
    desc: "Numbered, clickable source list with publisher, URL, and access date.",
  },
];

interface ExcelFlavorDef {
  id: ExcelFlavor;
  label: string;
  desc: string;
  sheets: string;
  iconPath: string;
}

const EXCEL_FLAVORS: ExcelFlavorDef[] = [
  {
    id: "cells",
    label: "Cell Explorer",
    desc: "All sized cells with TAM, confidence band, and method count.",
    sheets: "_README · cells · geographies · subcategories",
    iconPath:
      "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
  },
  {
    id: "triangulation",
    label: "Triangulation Detail",
    desc: "Every estimate with method, source, and a drillable source URL.",
    sheets: "_README · triangulation · sources · methods",
    iconPath:
      "M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71",
  },
  {
    id: "players",
    label: "Player Shares",
    desc: "Market participants ranked by share per cell, with role and bands.",
    sheets: "_README · player_shares · companies",
    iconPath:
      "M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75M9 7a4 4 0 100 8 4 4 0 000-8z",
  },
  {
    id: "assumptions",
    label: "Assumptions Ledger",
    desc: "Versioned assumptions with scope, unit, derivation, and superseded_by chain.",
    sheets: "_README · assumptions · cell_links",
    iconPath:
      "M4 19.5A2.5 2.5 0 016.5 17H20M4 19.5A2.5 2.5 0 016.5 22H20V2H6.5A2.5 2.5 0 004 4.5v15zM8 7h8M8 11h5",
  },
  {
    id: "full",
    label: "Full Dataset",
    desc: "All four subject sheets in one workbook. _README carries scope, timestamp, and methodology.",
    sheets: "_README · cells · triangulation · player_shares · assumptions",
    iconPath:
      "M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5",
  },
];

// ─── Utility ─────────────────────────────────────────────────────────────────

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return "Unexpected error — check the status page.";
}

// ─── Shared primitives ────────────────────────────────────────────────────────

function SvgIcon({
  d,
  className = "h-4 w-4",
}: {
  d: string;
  className?: string;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={`shrink-0 ${className}`}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d={d} />
    </svg>
  );
}

function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <span
      className={`inline-block animate-spin rounded-full border-2 border-brand border-t-transparent ${className}`}
      aria-hidden
    />
  );
}

/**
 * Download anchor rendered once the backend returns a download_url.
 * Uses `download` attribute + `target="_blank"` so browsers that can render
 * the file inline still offer a download prompt.
 */
function DownloadButton({
  url,
  label,
}: {
  url: string;
  label: string;
}) {
  return (
    <a
      href={url}
      download
      target="_blank"
      rel="noreferrer"
      className="focusable inline-flex items-center gap-1.5 rounded-md bg-confidence-high-bg px-3 py-1.5 text-xs font-medium text-confidence-high transition-colors hover:bg-emerald-100"
    >
      <SvgIcon
        d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1M7 10l5 5 5-5M12 15V3"
        className="h-3.5 w-3.5"
      />
      {label}
    </a>
  );
}

/**
 * Inline job status line: shows loading spinner, ready download link, or error
 * text below a generate/regenerate button.
 */
function JobStatusLine({
  job,
  downloadLabel,
}: {
  job: ReportJob;
  downloadLabel: string;
}) {
  if (job.status === "idle") return null;
  if (job.status === "loading")
    return (
      <div className="flex items-center gap-1.5 text-xs text-ink-muted">
        <Spinner className="h-3.5 w-3.5" />
        <span>Generating — this may take a few seconds…</span>
      </div>
    );
  if (job.status === "error")
    return (
      <p className="text-xs text-red-600" title={job.error}>
        {job.error ?? "Generation failed. Retry or check the Status page."}
      </p>
    );
  // ready
  return (
    <div className="flex flex-wrap items-center gap-2">
      <DownloadButton url={job.downloadUrl!} label={downloadLabel} />
      {job.generatedAt && (
        <span className="text-2xs text-ink-subtle">
          Generated {formatDate(job.generatedAt)}
        </span>
      )}
    </div>
  );
}

/** Primary action button for a report card. */
function GenerateButton({
  job,
  onClick,
  labelIdle,
  labelReady,
}: {
  job: ReportJob;
  onClick: () => void;
  labelIdle: string;
  labelReady: string;
}) {
  const loading = job.status === "loading";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={loading}
      className="focusable inline-flex shrink-0 items-center gap-1.5 rounded-md bg-brand px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {loading ? (
        <>
          <Spinner className="h-3.5 w-3.5" />
          Generating…
        </>
      ) : job.status === "ready" ? (
        <>
          <SvgIcon
            d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"
            className="h-3.5 w-3.5"
          />
          {labelReady}
        </>
      ) : (
        <>
          <SvgIcon
            d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8zM14 2v6h6"
            className="h-3.5 w-3.5"
          />
          {labelIdle}
        </>
      )}
    </button>
  );
}

// ─── Standard report card ─────────────────────────────────────────────────────

function StandardReportCard({
  def,
  job,
  onGenerate,
}: {
  def: StandardReportDef;
  job: ReportJob;
  onGenerate: () => void;
}) {
  return (
    <article className="card flex flex-col gap-4 p-5">
      {/* Header */}
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-brand-50 text-brand">
          <SvgIcon d={def.iconPath} className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-ink">{def.title}</h3>
            <span className="badge bg-surface-subtle text-2xs text-ink-subtle">
              PDF
            </span>
          </div>
          <p className="mt-1 text-xs leading-relaxed text-ink-muted">
            {def.desc}
          </p>
        </div>
      </div>

      {/* Scope note */}
      <p className="border-l-2 border-line pl-2.5 text-2xs text-ink-subtle">
        {def.note}
      </p>

      {/* Actions */}
      <div className="mt-auto space-y-3 border-t border-line pt-3">
        <JobStatusLine job={job} downloadLabel="Download PDF" />
        <div className="flex justify-end">
          <GenerateButton
            job={job}
            onClick={onGenerate}
            labelIdle="Generate PDF"
            labelReady="Regenerate"
          />
        </div>
      </div>
    </article>
  );
}

// ─── Excel export card ────────────────────────────────────────────────────────

function ExcelExportCard({
  def,
  job,
  onExport,
}: {
  def: ExcelFlavorDef;
  job: ReportJob;
  onExport: () => void;
}) {
  const loading = job.status === "loading";
  return (
    <article className="card flex flex-col gap-3 p-4">
      {/* Icon + title */}
      <div className="flex items-center gap-2.5">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-emerald-50 text-emerald-600">
          <SvgIcon d={def.iconPath} className="h-4 w-4" />
        </div>
        <h3 className="text-sm font-semibold text-ink">{def.label}</h3>
      </div>

      {/* Description */}
      <p className="text-xs leading-relaxed text-ink-muted">{def.desc}</p>

      {/* Sheet preview */}
      <p className="font-mono text-2xs text-ink-subtle">
        Sheets: {def.sheets}
      </p>

      {/* Status */}
      <div className="min-h-[1.5rem]">
        {job.status === "loading" && (
          <span className="flex items-center gap-1.5 text-xs text-ink-muted">
            <Spinner className="h-3.5 w-3.5" />
            Exporting…
          </span>
        )}
        {job.status === "ready" && job.downloadUrl && (
          <div className="flex flex-col gap-1">
            <DownloadButton url={job.downloadUrl} label="Download .xlsx" />
            {job.generatedAt && (
              <span className="text-2xs text-ink-subtle">
                {formatDate(job.generatedAt)}
              </span>
            )}
          </div>
        )}
        {job.status === "error" && (
          <p className="text-xs text-red-600" title={job.error}>
            {job.error ?? "Export failed."}
          </p>
        )}
      </div>

      {/* Button */}
      <button
        type="button"
        onClick={onExport}
        disabled={loading}
        className="focusable mt-auto inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-line bg-surface-raised px-3 py-1.5 text-xs font-medium text-ink-muted transition-colors hover:border-brand hover:text-brand disabled:cursor-not-allowed disabled:opacity-50"
      >
        {loading ? (
          <>
            <Spinner className="h-3.5 w-3.5" />
            Exporting…
          </>
        ) : job.status === "ready" ? (
          <>
            <SvgIcon
              d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"
              className="h-3.5 w-3.5"
            />
            Re-export
          </>
        ) : (
          <>
            <SvgIcon
              d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8zM14 2v6h6M9 13h6M9 17h6"
              className="h-3.5 w-3.5"
            />
            Export .xlsx
          </>
        )}
      </button>
    </article>
  );
}

// ─── Custom builder sub-components ───────────────────────────────────────────

function SectionPickerItem({
  section,
  inCart,
  onToggle,
}: {
  section: SectionDef;
  inCart: boolean;
  onToggle: () => void;
}) {
  return (
    <div
      className={`flex items-start gap-2 rounded-md px-2 py-1.5 transition-colors ${
        inCart ? "bg-brand-50" : "hover:bg-surface-subtle"
      }`}
    >
      <div className="min-w-0 flex-1">
        <p className="text-xs font-medium text-ink">{section.label}</p>
        <p className="text-2xs text-ink-subtle">{section.desc}</p>
      </div>
      <button
        type="button"
        onClick={onToggle}
        title={inCart ? "Remove from cart" : "Add to cart"}
        className={`focusable mt-0.5 shrink-0 rounded p-0.5 transition-colors ${
          inCart
            ? "text-brand hover:text-brand-700"
            : "text-ink-subtle hover:text-brand"
        }`}
      >
        {inCart ? (
          <SvgIcon d="M20 6L9 17l-5-5" className="h-4 w-4" />
        ) : (
          <SvgIcon d="M12 5v14M5 12h14" className="h-4 w-4" />
        )}
      </button>
    </div>
  );
}

function CartItem({
  section,
  index,
  total,
  onMoveUp,
  onMoveDown,
  onRemove,
}: {
  section: SectionDef | undefined;
  index: number;
  total: number;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
}) {
  return (
    <li className="flex items-center gap-2 rounded-md border border-line bg-surface-subtle px-2 py-1.5">
      <span className="w-5 shrink-0 text-right font-mono text-2xs text-ink-subtle">
        {index + 1}
      </span>
      <span className="min-w-0 flex-1 truncate text-xs font-medium text-ink">
        {section?.label ?? "(unknown section)"}
      </span>
      <div className="flex shrink-0 items-center gap-0.5">
        <button
          type="button"
          onClick={onMoveUp}
          disabled={index === 0}
          title="Move up"
          className="focusable rounded p-0.5 text-ink-subtle transition-colors hover:text-ink disabled:cursor-not-allowed disabled:opacity-30"
        >
          <SvgIcon d="M18 15l-6-6-6 6" className="h-3 w-3" />
        </button>
        <button
          type="button"
          onClick={onMoveDown}
          disabled={index === total - 1}
          title="Move down"
          className="focusable rounded p-0.5 text-ink-subtle transition-colors hover:text-ink disabled:cursor-not-allowed disabled:opacity-30"
        >
          <SvgIcon d="M6 9l6 6 6-6" className="h-3 w-3" />
        </button>
        <button
          type="button"
          onClick={onRemove}
          title="Remove"
          className="focusable rounded p-0.5 text-ink-subtle transition-colors hover:text-red-600"
        >
          <SvgIcon d="M18 6L6 18M6 6l12 12" className="h-3 w-3" />
        </button>
      </div>
    </li>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

type StandardJobMap = Record<StandardReportType, ReportJob>;
type ExcelJobMap = Record<ExcelFlavor, ReportJob>;

export function ReportsClient() {
  // Standard report jobs
  const [standardJobs, setStandardJobs] = useState<StandardJobMap>({
    "executive-audit": IDLE_JOB,
    "gap-analysis": IDLE_JOB,
    "player-shares": IDLE_JOB,
  });

  // Excel export jobs
  const [excelJobs, setExcelJobs] = useState<ExcelJobMap>({
    cells: IDLE_JOB,
    triangulation: IDLE_JOB,
    players: IDLE_JOB,
    assumptions: IDLE_JOB,
    full: IDLE_JOB,
  });

  // Custom builder state
  const [cartSections, setCartSections] = useState<string[]>([]);
  const [customJob, setCustomJob] = useState<ReportJob>(IDLE_JOB);

  // ── Standard report generation ──────────────────────────────────────────

  const handleGenerateStandard = useCallback(
    async (type: StandardReportType) => {
      setStandardJobs((prev) => ({
        ...prev,
        [type]: { status: "loading" },
      }));
      try {
        const result: ReportResult = await api.generateStandardReport(type);
        setStandardJobs((prev) => ({
          ...prev,
          [type]: {
            status: "ready",
            downloadUrl: result.download_url,
            generatedAt: result.generated_at,
          },
        }));
      } catch (err) {
        setStandardJobs((prev) => ({
          ...prev,
          [type]: { status: "error", error: errorMessage(err) },
        }));
      }
    },
    [],
  );

  // ── Excel export generation ─────────────────────────────────────────────

  const handleGenerateExcel = useCallback(async (flavor: ExcelFlavor) => {
    setExcelJobs((prev) => ({ ...prev, [flavor]: { status: "loading" } }));
    try {
      const result: ReportResult = await api.generateExcelExport(flavor);
      setExcelJobs((prev) => ({
        ...prev,
        [flavor]: {
          status: "ready",
          downloadUrl: result.download_url,
          generatedAt: result.generated_at,
        },
      }));
    } catch (err) {
      setExcelJobs((prev) => ({
        ...prev,
        [flavor]: { status: "error", error: errorMessage(err) },
      }));
    }
  }, []);

  // ── Cart management ─────────────────────────────────────────────────────

  const addSection = useCallback((id: string) => {
    setCartSections((prev) => (prev.includes(id) ? prev : [...prev, id]));
    // Invalidate previous custom output when cart changes
    setCustomJob(IDLE_JOB);
  }, []);

  const removeSection = useCallback((id: string) => {
    setCartSections((prev) => prev.filter((s) => s !== id));
    setCustomJob(IDLE_JOB);
  }, []);

  const moveSection = useCallback((id: string, dir: "up" | "down") => {
    setCartSections((prev) => {
      const idx = prev.indexOf(id);
      if (idx === -1) return prev;
      const arr = [...prev];
      if (dir === "up" && idx > 0) {
        [arr[idx - 1], arr[idx]] = [arr[idx], arr[idx - 1]];
      } else if (dir === "down" && idx < arr.length - 1) {
        [arr[idx], arr[idx + 1]] = [arr[idx + 1], arr[idx]];
      }
      return arr;
    });
    setCustomJob(IDLE_JOB);
  }, []);

  const clearCart = useCallback(() => {
    setCartSections([]);
    setCustomJob(IDLE_JOB);
  }, []);

  // ── Custom report generation ─────────────────────────────────────────────

  const handleGenerateCustom = useCallback(async () => {
    if (cartSections.length === 0) return;
    setCustomJob({ status: "loading" });
    try {
      const result: ReportResult = await api.generateCustomReport(cartSections);
      setCustomJob({
        status: "ready",
        downloadUrl: result.download_url,
        generatedAt: result.generated_at,
      });
    } catch (err) {
      setCustomJob({ status: "error", error: errorMessage(err) });
    }
  }, [cartSections]);

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <>
      <PageHeader
        eyebrow="Outputs"
        title="Reports"
        description="Generate standard PDFs, build a custom report from individual sections, or export to Excel. Every PDF includes a numbered clickable Sources page; every Excel export includes a _README sheet."
      />

      {/* ── 1. Standard Reports ─────────────────────────────────────────── */}
      <section aria-labelledby="std-reports-heading" className="mb-10">
        <h2
          id="std-reports-heading"
          className="eyebrow mb-4 text-ink-muted"
        >
          Standard Reports
        </h2>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-3">
          {STANDARD_REPORTS.map((def) => (
            <StandardReportCard
              key={def.type}
              def={def}
              job={standardJobs[def.type]}
              onGenerate={() => handleGenerateStandard(def.type)}
            />
          ))}
        </div>
      </section>

      {/* ── 2. Custom Report Builder ─────────────────────────────────────── */}
      <section aria-labelledby="custom-builder-heading" className="mb-10">
        <div className="mb-4 flex items-end justify-between gap-3">
          <div>
            <h2
              id="custom-builder-heading"
              className="eyebrow text-ink-muted"
            >
              Custom Report Builder
            </h2>
            <p className="mt-1 max-w-2xl text-xs text-ink-muted">
              Pick sections from the left panel, order them in the cart, then
              generate a bespoke PDF. The Sources &amp; References section is
              always appended if not explicitly included.
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
          {/* Section picker */}
          <div className="card flex flex-col gap-2 p-4">
            <h3 className="eyebrow mb-1 text-ink-muted">
              Available Sections ({AVAILABLE_SECTIONS.length})
            </h3>
            <div className="max-h-96 space-y-0.5 overflow-y-auto">
              {AVAILABLE_SECTIONS.map((section) => (
                <SectionPickerItem
                  key={section.id}
                  section={section}
                  inCart={cartSections.includes(section.id)}
                  onToggle={() =>
                    cartSections.includes(section.id)
                      ? removeSection(section.id)
                      : addSection(section.id)
                  }
                />
              ))}
            </div>
          </div>

          {/* Cart */}
          <div className="card flex flex-col gap-4 p-4">
            <div className="flex items-center justify-between">
              <h3 className="eyebrow text-ink-muted">
                Cart
                {cartSections.length > 0 && (
                  <span className="ml-1.5 font-normal normal-case text-ink-subtle">
                    · {cartSections.length} section
                    {cartSections.length !== 1 ? "s" : ""}
                  </span>
                )}
              </h3>
              {cartSections.length > 0 && (
                <button
                  type="button"
                  onClick={clearCart}
                  className="focusable text-2xs text-ink-subtle transition-colors hover:text-ink"
                >
                  Clear all
                </button>
              )}
            </div>

            {cartSections.length === 0 ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 py-12 text-center">
                <SvgIcon
                  d="M3 6h18M3 12h18M3 18h18"
                  className="h-8 w-8 text-ink-subtle opacity-30"
                />
                <p className="text-sm text-ink-subtle">
                  Add sections from the left panel.
                </p>
                <p className="max-w-48 text-2xs text-ink-subtle">
                  Sections will appear here in the order they will be printed.
                </p>
              </div>
            ) : (
              <ol className="max-h-72 space-y-1 overflow-y-auto">
                {cartSections.map((id, idx) => {
                  const section = AVAILABLE_SECTIONS.find((s) => s.id === id);
                  return (
                    <CartItem
                      key={id}
                      section={section}
                      index={idx}
                      total={cartSections.length}
                      onMoveUp={() => moveSection(id, "up")}
                      onMoveDown={() => moveSection(id, "down")}
                      onRemove={() => removeSection(id)}
                    />
                  );
                })}
              </ol>
            )}

            {/* Generate area */}
            <div className="mt-auto space-y-3 border-t border-line pt-4">
              {customJob.status !== "idle" && (
                <JobStatusLine
                  job={customJob}
                  downloadLabel="Download Custom PDF"
                />
              )}
              <button
                type="button"
                onClick={handleGenerateCustom}
                disabled={
                  cartSections.length === 0 || customJob.status === "loading"
                }
                className="focusable inline-flex w-full items-center justify-center gap-1.5 rounded-md bg-brand px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {customJob.status === "loading" ? (
                  <>
                    <Spinner className="h-4 w-4" />
                    Generating…
                  </>
                ) : customJob.status === "ready" ? (
                  <>
                    <SvgIcon
                      d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"
                      className="h-4 w-4"
                    />
                    Regenerate Custom PDF
                  </>
                ) : (
                  <>
                    <SvgIcon
                      d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8zM14 2v6h6"
                      className="h-4 w-4"
                    />
                    Generate Custom PDF
                    {cartSections.length > 0 &&
                      ` (${cartSections.length} section${cartSections.length !== 1 ? "s" : ""})`}
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* ── 3. Excel Exports ─────────────────────────────────────────────── */}
      <section aria-labelledby="excel-exports-heading">
        <div className="mb-4">
          <h2 id="excel-exports-heading" className="eyebrow text-ink-muted">
            Excel Exports
          </h2>
          <p className="mt-1 max-w-2xl text-xs text-ink-muted">
            Every workbook includes a{" "}
            <span className="font-mono text-2xs">_README</span> sheet with
            scope, timestamp, and methodology. Source URLs in the Triangulation
            sheet are hyperlinked.
          </p>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
          {EXCEL_FLAVORS.map((def) => (
            <ExcelExportCard
              key={def.id}
              def={def}
              job={excelJobs[def.id]}
              onExport={() => handleGenerateExcel(def.id)}
            />
          ))}
        </div>
      </section>
    </>
  );
}

export default ReportsClient;
