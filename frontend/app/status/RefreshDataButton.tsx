"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";

/**
 * "Refresh data" — re-pulls every enabled connector in the active engagement and
 * re-sizes cells from the fresh data (distinct from the page-refresh button,
 * which only re-fetches the current view). Run this after enabling new connectors
 * or whenever you want the model brought up to date.
 */
function readEngagementId(): string | null {
  if (typeof document === "undefined") return null;
  const m = document.cookie.match(/(?:^|;\s*)engagement_id=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

export function RefreshDataButton() {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handle() {
    setBusy(true);
    setNote(null);
    setError(null);
    try {
      let id = readEngagementId();
      if (!id) id = (await api.currentEngagement()).engagement_id;
      const res = await api.refreshEngagement(id);
      setNote(res.detail);
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : "Refresh failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={handle}
        disabled={busy}
        title="Re-pull all enabled connectors and re-size cells from the fresh data"
        className="focusable inline-flex items-center gap-1.5 rounded-md bg-brand px-3 py-1.5 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand/90 disabled:cursor-not-allowed disabled:opacity-60"
      >
        <svg viewBox="0 0 24 24" className={`h-4 w-4 shrink-0 ${busy ? "animate-spin" : ""}`}
             fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M23 4v6h-6M1 20v-6h6" />
          <path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" />
        </svg>
        {busy ? "Refreshing data…" : "Refresh data"}
      </button>
      {note && (
        <span className="max-w-xs text-right text-2xs text-emerald-700">{note}</span>
      )}
      {error && (
        <span className="max-w-xs text-right text-2xs text-red-600">{error}</span>
      )}
    </div>
  );
}

export default RefreshDataButton;
