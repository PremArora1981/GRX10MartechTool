/**
 * Sources View — W2 (client-priority screen).
 *
 * Lists every registered source with:
 *  - Class (A / B / C) — primary structured | cross-check | triangulation
 *  - Connector health badge (last_probe_status)
 *  - "Why it matters" rationale line (from notes or class default)
 *  - "Used by" method chips (derived from method_registry.required_raw_tables)
 *  - "Recommended sources by method" section
 *
 * Next.js App Router Server Component. NavShell is injected by app/layout.tsx.
 * Data is fetched in parallel on the server so first paint is populated.
 * Uses getCurrentUser() (not requireUser()) — renders anonymously in dev.
 */

import type { Metadata } from "next";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { SourcesClient } from "./SourcesClient";

export const metadata: Metadata = { title: "Sources" };

export default async function SourcesPage() {
  // Fetch both endpoints in parallel — backend handles empty tables gracefully.
  const [sources, recommended] = await Promise.all([
    api.listSourceDetails().catch(() => []),
    api.getRecommendedSources().catch(() => ({ method_map: {} })),
  ]);

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Data provenance"
        title="Sources"
        description={
          <>
            Every registered data source with its evidence class, connector
            health, and the triangulation methods it feeds. Class&nbsp;A sources
            qualify HIGH confidence; Class&nbsp;B qualifies MEDIUM; Class&nbsp;C
            provides gap-fill and scaling support.
          </>
        }
      />

      <SourcesClient sources={sources} recommended={recommended} />
    </div>
  );
}
