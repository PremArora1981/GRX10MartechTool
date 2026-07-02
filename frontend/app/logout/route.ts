import { redirect } from "next/navigation";
import { signOut } from "@/lib/auth";

/**
 * Sign-out route. Clears the AuthKit session and redirects to the login screen.
 *
 * When WorkOS is configured, `signOut` performs the redirect itself (it throws a
 * Next redirect). When WorkOS is not configured (local/demo) `signOut` is a
 * no-op and we redirect to /login ourselves.
 */
export async function GET(): Promise<Response> {
  await signOut({ returnTo: "/login" });
  redirect("/login");
}
