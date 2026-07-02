export const dynamic = "force-dynamic";
import type { Metadata } from "next";
import { getCurrentUser, canEditAssumptions } from "@/lib/auth";
import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components";
import AssumptionsLedgerClient from "./AssumptionsLedgerClient";

export const metadata: Metadata = { title: "Assumptions Ledger" };

// ---------------------------------------------------------------------------
// Error state (shown when the backend is unreachable at SSR time)
// ---------------------------------------------------------------------------

function BackendErrorState({ message }: { message: string }) {
  return (
    <main className="mx-auto max-w-screen-xl px-6 py-8">
      <PageHeader
        eyebrow="Layer 4"
        title="Assumptions Ledger"
        description="Versioned assumptions with reverse drill to influenced cells."
      />
      <div className="card flex flex-col items-center justify-center py-16 text-center">
        <p className="text-sm font-medium text-ink">
          Unable to load assumptions
        </p>
        <p className="mt-1 max-w-sm text-sm text-ink-muted">{message}</p>
      </div>
    </main>
  );
}

// ---------------------------------------------------------------------------
// Data loader (isolated so the page body stays readable)
// ---------------------------------------------------------------------------

async function fetchPageData() {
  const [assumptions, geographies, subcategories, companies, sources] =
    await Promise.all([
      api.listAssumptions(),
      api.listGeographies(),
      api.listSubcategories(),
      api.listCompanies(),
      api.listSources(),
    ]);
  return { assumptions, geographies, subcategories, companies, sources };
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default async function AssumptionsPage() {
  // getCurrentUser() degrades to null when WorkOS is not configured (local dev).
  // requireUser() would redirect to the WorkOS sign-in page, breaking local renders.
  const user = await getCurrentUser();

  let data: Awaited<ReturnType<typeof fetchPageData>>;
  try {
    data = await fetchPageData();
  } catch (err) {
    const msg =
      err instanceof ApiError
        ? `Backend returned ${err.status}: ${err.message}`
        : "The backend service is unreachable. Check the pipeline status and try again.";
    return <BackendErrorState message={msg} />;
  }

  const { assumptions, geographies, subcategories, companies, sources } = data;

  return (
    <main className="mx-auto max-w-screen-xl px-6 py-8">
      <PageHeader
        eyebrow="Layer 4"
        title="Assumptions Ledger"
        description="All analytical assumptions underpinning the model, kept as immutable versioned chains. Every prior version is preserved — changes are superseded, never overwritten."
      />
      <AssumptionsLedgerClient
        assumptions={assumptions}
        geographies={geographies}
        subcategories={subcategories}
        companies={companies}
        sources={sources}
        canEdit={canEditAssumptions(user?.role ?? undefined)}
        userRole={user?.role ?? "external"}
      />
    </main>
  );
}
