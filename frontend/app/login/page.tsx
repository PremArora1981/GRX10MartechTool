import type { Metadata } from "next";
import Link from "next/link";
import { getSignInUrl, getCurrentUser } from "@/lib/auth";
import { redirect } from "next/navigation";

export const metadata: Metadata = { title: "Sign in" };
export const dynamic = "force-dynamic";

/**
 * Login screen (Q10 — WorkOS managed auth).
 *
 * Layout: two-column on md+. Left panel carries brand context (platform name,
 * value pillars). Right panel renders the sign-in card. Both collapse to a
 * centred single card on mobile.
 *
 * If WorkOS env vars are absent (local dev), the button is gracefully disabled
 * with a configuration note rather than crashing. Already-authenticated users
 * are bounced to the dashboard before any UI renders.
 */
export default async function LoginPage() {
  const user = await getCurrentUser();
  if (user) redirect("/");

  let signInUrl: string | null = null;
  try {
    signInUrl = await getSignInUrl();
  } catch {
    signInUrl = null;
  }

  return (
    <div className="flex min-h-[80vh] items-stretch gap-0 overflow-hidden rounded-card border border-line shadow-raised md:my-8 md:mx-auto md:max-w-4xl">
      {/* ── Left branding panel (hidden on mobile) ── */}
      <div className="hidden flex-col justify-between bg-surface-inverse px-10 py-12 md:flex md:w-2/5">
        {/* Logo */}
        <div>
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand text-sm font-bold text-white">
              G
            </div>
            <div className="leading-tight">
              <div className="text-sm font-semibold text-white">GRX10</div>
              <div className="text-2xs text-white/55">Market Research</div>
            </div>
          </div>

          <h1 className="mt-8 text-xl font-semibold leading-snug text-white">
            Automated market sizing — source-traceable, triangulated, auditable.
          </h1>

          {/* Value pillars */}
          <ul className="mt-6 space-y-4">
            {[
              {
                icon: "M9 12l2 2 4-4M7.835 4.697a3.42 3.42 0 001.946-.806 3.42 3.42 0 014.438 0 3.42 3.42 0 001.946.806 3.42 3.42 0 013.138 3.138 3.42 3.42 0 00.806 1.946 3.42 3.42 0 010 4.438 3.42 3.42 0 00-.806 1.946 3.42 3.42 0 01-3.138 3.138 3.42 3.42 0 00-1.946.806 3.42 3.42 0 01-4.438 0 3.42 3.42 0 00-1.946-.806 3.42 3.42 0 01-3.138-3.138 3.42 3.42 0 00-.806-1.946 3.42 3.42 0 010-4.438 3.42 3.42 0 00.806-1.946 3.42 3.42 0 013.138-3.138z",
                label: "Every estimate drills to its raw source in two clicks",
              },
              {
                icon: "M13 10V3L4 14h7v7l9-11h-7z",
                label: "Configurable confidence: Light → Audit-grade validation profiles",
              },
              {
                icon: "M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z",
                label: "Role-aware: analyst, business, and external views built in",
              },
            ].map(({ icon, label }) => (
              <li key={label} className="flex items-start gap-3">
                <svg
                  viewBox="0 0 24 24"
                  className="mt-0.5 h-4 w-4 shrink-0 text-brand-200"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.7}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden
                >
                  <path d={icon} />
                </svg>
                <span className="text-sm text-white/75">{label}</span>
              </li>
            ))}
          </ul>
        </div>

        <p className="text-2xs text-white/30">
          &copy; {new Date().getFullYear()} GRX10 Solutions Private Limited
        </p>
      </div>

      {/* ── Right sign-in panel ── */}
      <div className="flex flex-1 flex-col items-center justify-center bg-surface px-8 py-12">
        {/* Mobile logo (shown only when left panel is hidden) */}
        <div className="mb-6 flex items-center gap-2 md:hidden">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand text-sm font-bold text-white">
            G
          </div>
          <div>
            <div className="text-sm font-semibold text-ink">GRX10</div>
            <div className="text-2xs text-ink-subtle">Market Research</div>
          </div>
        </div>

        <div className="w-full max-w-xs">
          <h2 className="text-xl font-semibold text-ink">Sign in</h2>
          <p className="mt-1.5 text-sm text-ink-muted">
            Access triangulated, source-traceable market sizing for your
            engagement.
          </p>

          {signInUrl ? (
            <>
              <a
                href={signInUrl}
                className="focusable mt-6 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-brand px-4 py-2.5 text-sm font-medium text-white shadow-sm hover:bg-brand-700 active:bg-brand-900"
              >
                {/* WorkOS icon placeholder — simple key glyph */}
                <svg
                  viewBox="0 0 24 24"
                  className="h-4 w-4"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden
                >
                  <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 11-7.778 7.778 5.5 5.5 0 017.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
                </svg>
                Continue with WorkOS
              </a>

              <div className="mt-4 flex items-center gap-3">
                <div className="h-px flex-1 bg-line" />
                <span className="text-2xs text-ink-subtle">
                  SAML · OIDC · Google · Email
                </span>
                <div className="h-px flex-1 bg-line" />
              </div>

              <p className="mt-3 text-center text-2xs text-ink-subtle">
                SSO is managed by WorkOS. Contact your organization admin if you
                can&apos;t sign in.
              </p>
            </>
          ) : (
            <>
              <div className="mt-6">
                <button
                  disabled
                  className="w-full cursor-not-allowed rounded-lg bg-surface-subtle px-4 py-2.5 text-sm font-medium text-ink-subtle"
                >
                  SSO not configured
                </button>
                <p className="mt-2 text-center text-2xs text-ink-subtle">
                  Set{" "}
                  <code className="font-mono">WORKOS_API_KEY</code>,{" "}
                  <code className="font-mono">WORKOS_CLIENT_ID</code>, and{" "}
                  <code className="font-mono">WORKOS_REDIRECT_URI</code> to
                  enable sign-in.
                </p>
              </div>

              {/* Dev bypass: lets screen agents work without live WorkOS */}
              <Link
                href="/"
                className="focusable mt-5 inline-block w-full text-center text-xs text-ink-muted underline-offset-2 hover:text-ink hover:underline"
              >
                Continue to dashboard (dev mode)
              </Link>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
