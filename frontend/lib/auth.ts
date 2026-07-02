/**
 * WorkOS AuthKit integration + role mapping (Q10).
 *
 * Roles flow from the customer IdP -> WorkOS organization role -> our four
 * application roles: owner/admin · analyst · business · external. `owner_admin`
 * gates connector-credential entry (Q9) and the Phase-3 audience switcher.
 *
 * Everything here is server-only (AuthKit reads the encrypted session cookie).
 * Client components receive the resolved role as a prop, never call these.
 *
 * LOCAL / DEMO DEGRADATION
 * ------------------------
 * `@workos-inc/authkit-nextjs` is only loaded at runtime, and only when WorkOS
 * env vars are present. When they are absent (local dev / the client demo) the
 * module is never imported and every screen degrades to a mock **owner/admin**
 * session so admin surfaces render. This keeps local rendering unblocked and
 * avoids a hard build-time dependency on the optional `@workos-inc/node` peer.
 */

import "server-only";
import type { AppRole } from "./types";

/** True when the WorkOS environment is configured (production / staging). */
function workosConfigured(): boolean {
  return Boolean(process.env.WORKOS_API_KEY && process.env.WORKOS_CLIENT_ID);
}

/**
 * Runtime-load AuthKit without letting the bundler statically resolve it.
 * The specifier is held in a variable and flagged `webpackIgnore` so Next's
 * webpack never tries to bundle `@workos-inc/authkit-nextjs` (whose optional
 * `@workos-inc/node` peer may not be installed locally). Returns `null` when
 * WorkOS is not configured or the package cannot be loaded.
 */
async function loadAuthkit(): Promise<
  typeof import("@workos-inc/authkit-nextjs") | null
> {
  if (!workosConfigured()) return null;
  try {
    const specifier = "@workos-inc/authkit-nextjs";
    return (await import(/* webpackIgnore: true */ specifier)) as typeof import("@workos-inc/authkit-nextjs");
  } catch {
    return null;
  }
}

/** Human-readable labels for each role (used in the nav + settings). */
export const ROLE_LABELS: Record<AppRole, string> = {
  owner_admin: "Owner / Admin",
  analyst: "Analyst",
  business: "Business",
  external: "External",
};

/**
 * Map a raw WorkOS/IdP role slug onto an application role. Unknown roles fall
 * back to `external` (least-privileged) so a mis-mapped IdP claim can never
 * accidentally unlock admin surfaces.
 */
export function mapRole(raw: string | null | undefined): AppRole {
  const slug = (raw ?? "").trim().toLowerCase().replace(/[\s/]+/g, "_");
  switch (slug) {
    case "owner":
    case "admin":
    case "owner_admin":
    case "administrator":
      return "owner_admin";
    case "analyst":
    case "researcher":
      return "analyst";
    case "business":
    case "member":
    case "viewer":
      return "business";
    case "external":
    case "guest":
    case "partner":
      return "external";
    default:
      return "external";
  }
}

export interface SessionUser {
  id: string;
  email: string;
  firstName: string | null;
  lastName: string | null;
  /** Best-effort display name. */
  name: string;
  role: AppRole;
  organizationId: string | null;
}

/**
 * Mock owner/admin session used whenever WorkOS is not configured. Lets the
 * full application (including admin-gated surfaces) render locally and in the
 * demo without a live IdP.
 */
const MOCK_OWNER: SessionUser = {
  id: "local-owner",
  email: "owner@grx10.local",
  firstName: "Local",
  lastName: "Owner",
  name: "Local Owner",
  role: "owner_admin",
  organizationId: "local-org",
};

function displayName(
  first: string | null,
  last: string | null,
  email: string,
): string {
  const full = [first, last].filter(Boolean).join(" ").trim();
  return full || email;
}

/**
 * Resolve the current session. Returns the mock owner when WorkOS is not
 * configured (keeps local screen development + the demo unblocked), or `null`
 * when WorkOS is configured but there is no active session.
 */
export async function getCurrentUser(): Promise<SessionUser | null> {
  const kit = await loadAuthkit();
  if (!kit) return MOCK_OWNER;
  try {
    const { user, role, organizationId } = await kit.withAuth();
    if (!user) return null;
    return {
      id: user.id,
      email: user.email,
      firstName: user.firstName ?? null,
      lastName: user.lastName ?? null,
      name: displayName(user.firstName ?? null, user.lastName ?? null, user.email),
      role: mapRole(role),
      organizationId: organizationId ?? null,
    };
  } catch {
    // No session cookie or WorkOS runtime error — treat as anonymous.
    return null;
  }
}

/**
 * Like {@link getCurrentUser} but guarantees a user. When WorkOS is configured
 * it redirects to sign-in for anonymous requests; when it is not configured it
 * returns the mock owner so local/demo pages render instead of redirecting.
 */
export async function requireUser(): Promise<SessionUser> {
  const kit = await loadAuthkit();
  if (!kit) return MOCK_OWNER;
  const { user, role, organizationId } = await kit.withAuth({
    ensureSignedIn: true,
  });
  // ensureSignedIn guarantees a user (it redirects otherwise).
  return {
    id: user!.id,
    email: user!.email,
    firstName: user!.firstName ?? null,
    lastName: user!.lastName ?? null,
    name: displayName(user!.firstName ?? null, user!.lastName ?? null, user!.email),
    role: mapRole(role),
    organizationId: organizationId ?? null,
  };
}

/**
 * WorkOS hosted sign-in URL. Throws when WorkOS is not configured; callers
 * (the login screen) catch this and render an "SSO not configured" state.
 */
export async function getSignInUrl(): Promise<string> {
  const kit = await loadAuthkit();
  if (!kit) throw new Error("WorkOS is not configured");
  return kit.getSignInUrl();
}

/** WorkOS hosted sign-up URL. Throws when WorkOS is not configured. */
export async function getSignUpUrl(): Promise<string> {
  const kit = await loadAuthkit();
  if (!kit) throw new Error("WorkOS is not configured");
  return kit.getSignUpUrl();
}

/**
 * Sign out of the AuthKit session. When WorkOS is not configured this is a
 * no-op (the caller should redirect to /login itself).
 */
export async function signOut(options?: { returnTo?: string }): Promise<void> {
  const kit = await loadAuthkit();
  if (!kit) return;
  await kit.signOut(options);
}

// ---------------------------------------------------------------------------
// Capability helpers — gate UI by role. Keep authz decisions centralized here.
// ---------------------------------------------------------------------------

/** Only owner/admin may enter or rotate connector credentials (Q9). */
export function canEnterCredentials(role: AppRole | null | undefined): boolean {
  return role === "owner_admin";
}

/** Owner/admin may change the active validation profile + web-search toggle. */
export function canManageSettings(role: AppRole | null | undefined): boolean {
  return role === "owner_admin";
}

/** Analysts and above can author assumptions / commentary. */
export function canEditAssumptions(role: AppRole | null | undefined): boolean {
  return role === "owner_admin" || role === "analyst";
}

/** External users see published outputs only (no raw drill, no admin). */
export function canDrillToRaw(role: AppRole | null | undefined): boolean {
  return role !== "external" && role != null;
}

/** Generic precedence check: does `role` meet `minimum`? */
const ROLE_RANK: Record<AppRole, number> = {
  external: 0,
  business: 1,
  analyst: 2,
  owner_admin: 3,
};
export function hasAtLeast(
  role: AppRole | null | undefined,
  minimum: AppRole,
): boolean {
  if (!role) return false;
  return ROLE_RANK[role] >= ROLE_RANK[minimum];
}
