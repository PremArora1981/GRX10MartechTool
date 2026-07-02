import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

/**
 * WorkOS AuthKit middleware — loaded ONLY when WorkOS is configured.
 *
 * Mirrors lib/auth.ts's degradation contract: when WORKOS_API_KEY /
 * WORKOS_CLIENT_ID are absent (local dev, the client demo, or a fresh Render
 * deploy before SSO onboarding) every request passes through and screens
 * resolve the mock owner session. When WorkOS IS configured, AuthKit keeps the
 * encrypted session cookie fresh on every request. `middlewareAuth.enabled =
 * false` means routes are not force-gated at the edge — individual screens
 * decide access via `requireUser()` / role helpers in lib/auth.ts.
 */

const workosConfigured = Boolean(
  process.env.WORKOS_API_KEY && process.env.WORKOS_CLIENT_ID,
);

// Held in a variable + webpackIgnore so the bundler never statically resolves
// the optional @workos-inc/* dependency tree (same pattern as lib/auth.ts).
async function loadAuthkitMiddleware() {
  const specifier = "@workos-inc/authkit-nextjs";
  const mod = (await import(
    /* webpackIgnore: true */ specifier
  )) as typeof import("@workos-inc/authkit-nextjs");
  return mod.authkitMiddleware({
    middlewareAuth: {
      enabled: false,
      unauthenticatedPaths: ["/login", "/callback"],
    },
  });
}

const authkit = workosConfigured ? loadAuthkitMiddleware() : null;

export default async function middleware(request: NextRequest) {
  if (!authkit) return NextResponse.next();
  try {
    const handler = await authkit;
    return handler(request, {} as never);
  } catch {
    // AuthKit failed to load — degrade to pass-through rather than 500 the app.
    return NextResponse.next();
  }
}

export const config = {
  // Run on everything except Next internals and static assets.
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)"],
};
