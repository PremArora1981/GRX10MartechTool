import { NextResponse } from "next/server";

/**
 * WorkOS AuthKit OAuth callback. WorkOS redirects here (WORKOS_REDIRECT_URI)
 * after the user authenticates; `handleAuth` exchanges the code, sets the
 * session cookie, and forwards the user on. Default landing is the dashboard.
 *
 * AuthKit is imported at runtime (never statically bundled) so the build does
 * not hard-depend on the optional `@workos-inc/node` peer. When WorkOS is not
 * configured (local/demo) the callback simply redirects home.
 */
export async function GET(request: Request): Promise<Response> {
  if (!(process.env.WORKOS_API_KEY && process.env.WORKOS_CLIENT_ID)) {
    return NextResponse.redirect(new URL("/", request.url));
  }
  try {
    const specifier = "@workos-inc/authkit-nextjs";
    const kit = (await import(/* webpackIgnore: true */ specifier)) as typeof import("@workos-inc/authkit-nextjs");
    return kit.handleAuth({ returnPathname: "/" })(request);
  } catch {
    return NextResponse.redirect(new URL("/", request.url));
  }
}
