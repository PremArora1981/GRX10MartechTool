import Link from "next/link";
import { PageHeader } from "@/components/PageHeader";
import { api, ApiError } from "@/lib/api";
import { formatUsdMillions } from "@/lib/format";
import type { StatsOverview } from "@/lib/api";
import type { Engagement } from "@/lib/types";
import DashboardCharts from "./_components/DashboardCharts";

export const dynamic = "force-dynamic";

/**
 * Dashboard landing — the demo's opening screen (W1).
 *
 * Server component: fetches GET /stats/overview server-side so the initial
 * paint is fully populated with real data. Passes the payload to DashboardCharts
 * (client component) for the Recharts visualisations.
 *
 * Degrades gracefully: if the API is unreachable the stat cards show "—" and a
 * warning banner is shown; chart area shows a placeholder card.
 */
export default async function DashboardPage() {
  let overview: StatsOverview | null = null;
  let engagement: Engagement | null = null;
  let apiError: string | null = null;

  try {
    [overview, engagement] = await Promise.all([
      api.getStatsOverview(),          // backend picks the engagement's latest year
      api.currentEngagement().catch(() => null),
    ]);
  } catch (err) {
    apiError =
      err instanceof ApiError
        ? `API ${err.status || "unreachable"}: ${err.message}`
        : "Backend not reachable yet.";
  }

  const cb = overview?.confidence_breakdown;
  const year = overview?.year ?? new Date().getFullYear();
  const engName = engagement?.name ?? "Market Research";
  const nGeos = overview?.by_geography?.length ?? 0;
  const nCells = overview?.cell_count ?? 0;

  return (
    <>
      <PageHeader
        eyebrow={`Overview · ${year}`}
        title={`${engName} — Market Research Dashboard`}
        description={
          nCells > 0
            ? `${nCells.toLocaleString()} market cells for ${year} across ${nGeos} ${nGeos === 1 ? "geography" : "geographies"}. Every number drillable to a source URL.`
            : "No sized cells yet for this engagement — run the pipeline or web-search populate to fill the model."
        }
      />

      {apiError && (
        <div className="card mb-6 border-health-auth/40 bg-health-auth-bg p-4 text-sm text-ink-muted">
          <span className="font-medium text-ink">Awaiting data — </span>
          {apiError} Screens render as soon as the FastAPI service responds.
        </div>
      )}

      {/* ── Headline stat cards ──────────────────────────────────────────── */}
      <section
        aria-label="Headline statistics"
        className="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5"
      >
        <StatCard
          label={`Total TAM ${year}`}
          value={overview ? formatUsdMillions(overview.total_tam_usd_m) : "—"}
          sub={`across ${nGeos} ${nGeos === 1 ? "geography" : "geographies"}`}
        />
        <StatCard
          label="Market cells"
          value={overview ? overview.cell_count.toLocaleString() : "—"}
          sub={`${year} model year`}
          href="/cells"
        />
        <StatCard
          label="High confidence"
          value={cb ? cb.high.count.toLocaleString() : "—"}
          sub={cb ? formatUsdMillions(cb.high.tam_usd_m) : "—"}
          tone="high"
          href="/cells?confidence=high"
        />
        <StatCard
          label="Medium confidence"
          value={cb ? cb.medium.count.toLocaleString() : "—"}
          sub={cb ? formatUsdMillions(cb.medium.tam_usd_m) : "—"}
          tone="medium"
          href="/cells?confidence=medium"
        />
        <StatCard
          label="Low confidence"
          value={cb ? cb.low.count.toLocaleString() : "—"}
          sub={cb ? formatUsdMillions(cb.low.tam_usd_m) : "—"}
          tone="low"
          href="/cells?confidence=low"
        />
      </section>

      {/* ── Charts (client component, data passed as prop) ───────────────── */}
      {overview ? (
        <DashboardCharts overview={overview} />
      ) : (
        <div className="card mb-6 p-10 text-center">
          <p className="text-sm text-ink-subtle">
            Charts will appear once the backend responds.
          </p>
        </div>
      )}

      {/* ── Quick links ──────────────────────────────────────────────────── */}
      <section
        aria-label="Quick navigation"
        className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6"
      >
        {(
          [
            ["Cell Explorer", "/cells"],
            ["Players", "/players"],
            ["Connectors", "/connectors"],
            ["Assumptions", "/assumptions"],
            ["Reports", "/reports"],
            ["Settings", "/settings"],
          ] as [string, string][]
        ).map(([label, href]) => (
          <Link
            key={href}
            href={href}
            className="focusable card px-4 py-3 text-sm font-medium text-ink transition-colors hover:bg-surface-subtle"
          >
            {label}
          </Link>
        ))}
      </section>
    </>
  );
}

// ---------------------------------------------------------------------------
// StatCard sub-component (server-renderable, no interactivity)
// ---------------------------------------------------------------------------

function StatCard({
  label,
  value,
  sub,
  href,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  href?: string;
  tone?: "high" | "medium" | "low";
}) {
  const toneCls =
    tone === "high"
      ? "text-confidence-high"
      : tone === "medium"
        ? "text-confidence-medium"
        : tone === "low"
          ? "text-confidence-low"
          : "text-ink";

  const inner = (
    <>
      <div className="eyebrow">{label}</div>
      <div className={`tnum mt-1 text-2xl font-semibold ${toneCls}`}>
        {value}
      </div>
      {sub && (
        <div className="mt-0.5 truncate text-xs text-ink-subtle">{sub}</div>
      )}
    </>
  );

  if (href) {
    return (
      <Link
        href={href}
        className="focusable card p-4 transition-colors hover:bg-surface-subtle"
      >
        {inner}
      </Link>
    );
  }
  return <div className="card p-4">{inner}</div>;
}
