"use client";

import { useRouter } from "next/navigation";
import { useTransition } from "react";

/**
 * Client component: re-runs all server-side data fetches for the status page
 * without a full navigation, ensuring always-live freshness (cache: 'no-store'
 * in api.getStatus already ensures no edge-cache; this triggers a fresh RSC
 * re-render on demand).
 */
export function StatusRefreshButton() {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  function handleRefresh() {
    startTransition(() => {
      router.refresh();
    });
  }

  return (
    <button
      onClick={handleRefresh}
      disabled={isPending}
      className="focusable inline-flex items-center gap-1.5 rounded-md border border-line bg-surface px-3 py-1.5 text-sm font-medium text-ink shadow-sm transition-colors hover:bg-surface-subtle disabled:cursor-not-allowed disabled:opacity-60"
      aria-label="Refresh status snapshot"
    >
      {/* Rotation animation while pending */}
      <svg
        viewBox="0 0 24 24"
        className={`h-4 w-4 shrink-0 transition-transform ${isPending ? "animate-spin" : ""}`}
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M1 4v6h6M23 20v-6h-6" />
        <path d="M20.49 9A9 9 0 005.64 5.64L1 10M23 14l-4.64 4.36A9 9 0 013.51 15" />
      </svg>
      {isPending ? "Refreshing…" : "Refresh"}
    </button>
  );
}

export default StatusRefreshButton;
