import type { Metadata } from "next";
import { getCurrentUser, canManageSettings } from "@/lib/auth";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { SettingsClient } from "./SettingsClient";
import type { ValidationProfile } from "@/lib/types";

export const metadata: Metadata = { title: "Settings" };

/**
 * Settings screen (v1 App Screen 9).
 *
 * Fetch-only RSC: resolves the session, pulls validation profiles and current
 * settings, then hands initial state to the interactive `SettingsClient`.
 * All three backend endpoints (web-search, audience-pref) degrade gracefully
 * if the backend is not yet running — sensible defaults are used.
 *
 * Write actions live in SettingsClient (client component) which calls the
 * backend directly so mutations don't require a full page reload.
 */
export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const user = await getCurrentUser();
  const canManage = canManageSettings(user?.role);

  // Resolve all settings in parallel. Each block degrades to a safe default
  // so the page renders even when the backend is not yet wired up.
  const [profilesResult, webSearchResult, audienceResult] =
    await Promise.allSettled([
      api.listValidationProfiles(),
      api.getWebSearchConfig(),
      api.getAudiencePref(),
    ]);

  const profiles: ValidationProfile[] =
    profilesResult.status === "fulfilled" ? profilesResult.value : [];

  // Q8: web-search fallback is on by default.
  const webSearchEnabled: boolean =
    webSearchResult.status === "fulfilled"
      ? webSearchResult.value.enabled
      : true;

  // Default audience: match the signed-in user's role, or "all" for admins.
  const defaultAudience =
    user?.role === "analyst"
      ? "analyst"
      : user?.role === "business"
        ? "business"
        : user?.role === "external"
          ? "external"
          : "all";

  const initialAudience: string =
    audienceResult.status === "fulfilled"
      ? audienceResult.value.audience
      : defaultAudience;

  return (
    <div>
      <PageHeader
        eyebrow="Configuration"
        title="Settings"
        description={
          canManage
            ? "Manage validation profiles, web-search fallback, and audience preview for this engagement."
            : "View the current engagement configuration. Owner / Admin role required to make changes."
        }
        actions={
          user && (
            <span className="badge bg-surface-subtle text-ink-muted">
              {user.name} · {user.role.replace("_", " / ")}
            </span>
          )
        }
      />
      <SettingsClient
        initialProfiles={profiles}
        initialWebSearch={webSearchEnabled}
        initialAudience={initialAudience}
        canManage={canManage}
        userRole={user?.role ?? null}
      />
    </div>
  );
}
