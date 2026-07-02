"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import type { AppRole } from "@/lib/types";

/**
 * Persistent left-nav application shell. Links the nine v1 screens, highlights
 * the active route, and shows the signed-in user + role at the foot. Screen
 * agents render their content into `children`; they do not re-create chrome.
 */

export interface NavUser {
  name: string;
  email: string;
  role: AppRole;
  roleLabel: string;
}

export interface NavShellProps {
  user: NavUser | null;
  children: ReactNode;
}

interface NavItem {
  href: string;
  label: string;
  icon: ReactNode;
  /** Hide from the nav for these roles. */
  hideFor?: AppRole[];
}

// Simple inline glyphs keep the bundle dependency-free.
const I = (d: string): ReactNode => (
  <svg
    viewBox="0 0 24 24"
    className="h-4 w-4 shrink-0"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.7}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden
  >
    <path d={d} />
  </svg>
);

const PRIMARY_NAV: NavItem[] = [
  { href: "/brief", label: "New Brief", icon: I("M11 4H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2v-4M18.5 2.5a2.121 2.121 0 013 3L13 14l-4 1 1-4 7.5-7.5z") },
  { href: "/", label: "Dashboard", icon: I("M3 12l9-9 9 9M5 10v10h14V10") },
  { href: "/cells", label: "Cell Explorer", icon: I("M4 4h16v16H4zM4 9h16M9 9v11M15 9v11") },
  { href: "/players", label: "Players", icon: I("M16 11a4 4 0 10-8 0M2 21a8 8 0 0120 0") },
  { href: "/connectors", label: "Connectors", icon: I("M6 9V6a3 3 0 016 0v3M9 12v6M5 12h14v9H5z") },
  { href: "/sources", label: "Sources", icon: I("M12 2a9 9 0 100 18A9 9 0 0012 2zM2 12h20M12 2a15 15 0 010 18M12 2a15 15 0 000 18") },
  { href: "/assumptions", label: "Assumptions Ledger", icon: I("M8 6h11M8 12h11M8 18h11M3 6h.01M3 12h.01M3 18h.01") },
  { href: "/reports", label: "Reports", icon: I("M7 3h7l5 5v13H7zM14 3v5h5M9 13h6M9 17h6") },
  { href: "/status", label: "Status", icon: I("M3 12h4l3 8 4-16 3 8h4") },
  { href: "/settings", label: "Settings", icon: I("M12 15a3 3 0 100-6 3 3 0 000 6zM19 12a7 7 0 00-.1-1l2-1.6-2-3.4-2.4 1a7 7 0 00-1.7-1L14.5 2h-5l-.3 2.4a7 7 0 00-1.7 1l-2.4-1-2 3.4L3.1 11a7 7 0 000 2l-2 1.6 2 3.4 2.4-1a7 7 0 001.7 1l.3 2.4h5l.3-2.4a7 7 0 001.7-1l2.4 1 2-3.4-2-1.6a7 7 0 00.1-1z"), hideFor: ["business", "external"] },
];

function NavLink({ item, active }: { item: NavItem; active: boolean }) {
  return (
    <Link
      href={item.href}
      className={`focusable flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
        active
          ? "bg-white/10 font-medium text-white"
          : "text-white/80 hover:bg-white/5 hover:text-white"
      }`}
      aria-current={active ? "page" : undefined}
    >
      {item.icon}
      <span className="truncate">{item.label}</span>
    </Link>
  );
}

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function NavShell({ user, children }: NavShellProps) {
  const pathname = usePathname() ?? "/";
  const items = PRIMARY_NAV.filter(
    (item) => !item.hideFor || !user || !item.hideFor.includes(user.role),
  );

  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col bg-surface-inverse text-ink-inverse">
        {/* Brand */}
        <div className="flex items-center gap-2.5 px-5 py-5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand text-sm font-bold text-white">
            G
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold text-white">GRX10</div>
            <div className="text-2xs text-white/55">Market Research</div>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 space-y-0.5 overflow-y-auto px-3 py-2">
          {items.map((item) => (
            <NavLink
              key={item.href}
              item={item}
              active={isActive(pathname, item.href)}
            />
          ))}
        </nav>

        {/* User / auth */}
        <div className="border-t border-white/10 px-3 py-3">
          {user ? (
            <div className="flex items-center gap-3 px-2">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand/30 text-xs font-semibold text-white">
                {user.name.slice(0, 2).toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-xs font-medium text-white">
                  {user.name}
                </div>
                <div className="truncate text-2xs text-white/55">
                  {user.roleLabel}
                </div>
              </div>
              <Link
                href="/logout"
                className="focusable rounded p-1 text-white/55 hover:text-white"
                title="Sign out"
              >
                {I("M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9")}
              </Link>
            </div>
          ) : (
            <Link
              href="/login"
              className="focusable flex items-center justify-center gap-2 rounded-lg bg-brand px-3 py-2 text-sm font-medium text-white hover:bg-brand-700"
            >
              Sign in
            </Link>
          )}
        </div>
      </aside>

      {/* Content */}
      <main className="min-w-0 flex-1">
        <div className="mx-auto max-w-7xl px-6 py-6">{children}</div>
      </main>
    </div>
  );
}

export default NavShell;
