import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { NavShell } from "@/components/NavShell";
import { getCurrentUser, ROLE_LABELS } from "@/lib/auth";

export const metadata: Metadata = {
  title: {
    default: "GRX10 Market Research",
    template: "%s · GRX10 Market Research",
  },
  description:
    "GRX10 Automated Market Research Tool — source-traceable, triangulated market sizing.",
};

/**
 * Root layout. Resolves the WorkOS session server-side and renders the
 * persistent left-nav shell around every screen. Individual screens are server
 * components by default; they opt into client interactivity as needed.
 */
export default async function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  const user = await getCurrentUser();
  const navUser = user
    ? {
        name: user.name,
        email: user.email,
        role: user.role,
        roleLabel: ROLE_LABELS[user.role],
      }
    : null;

  return (
    <html lang="en">
      <head>
        {/* GRX10 brand fonts — graceful fallback to system fonts offline. */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Montserrat:wght@600;700;800&family=Open+Sans:wght@400;500;600;700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <NavShell user={navUser}>{children}</NavShell>
      </body>
    </html>
  );
}
