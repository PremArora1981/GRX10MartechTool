export const dynamic = "force-dynamic";
/**
 * Connectors admin screen — server component entry point.
 *
 * Resolves the WorkOS session (redirects to sign-in if absent), fetches the
 * initial source catalog, and hands it to the interactive ConnectorsClient.
 * All UX state (filtering, drawer, modals) lives in the client component so
 * this file stays thin and cacheable.
 */

import type { Metadata } from "next";
import { requireUser, canEnterCredentials } from "@/lib/auth";
import { api } from "@/lib/api";
import { ConnectorsClient } from "./ConnectorsClient";

export const metadata: Metadata = { title: "Connectors" };

export default async function ConnectorsPage() {
  // Enforce authentication — redirects to WorkOS sign-in if no session.
  const user = await requireUser();

  // Prefetch the full source catalog on the server so the first paint is
  // sub-second (acceptance criterion). The client uses SWR fallback data.
  const sources = await api.listSources();

  return (
    <ConnectorsClient
      initialSources={sources}
      canEnterCredentials={canEnterCredentials(user.role)}
      role={user.role}
    />
  );
}
