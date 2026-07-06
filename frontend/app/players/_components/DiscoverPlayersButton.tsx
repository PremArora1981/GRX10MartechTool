"use client";

/**
 * DiscoverPlayersButton — client affordance for empty (new-vertical) verticals.
 *
 * The Players screen shows player shares per cell, but is empty for engagements
 * that have no player data yet. This button kicks off the AI player-discovery
 * job (POST /engagements/{id}/populate-players) and surfaces the returned
 * `detail` message, telling the user the work runs in the background and to
 * refresh in a few minutes.
 *
 * The active engagement is resolved from api.currentEngagement(), falling back
 * to the `engagement_id` cookie.
 */

import { useState } from "react";
import { api } from "@/lib/api";

function engagementIdFromCookie(): string | null {
  if (typeof document === "undefined") return null;
  const m = document.cookie.match(/(?:^|;\s*)engagement_id=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

async function resolveEngagementId(): Promise<string | null> {
  try {
    const eng = await api.currentEngagement();
    if (eng?.engagement_id) return eng.engagement_id;
  } catch {
    // fall through to cookie
  }
  return engagementIdFromCookie();
}

export interface DiscoverPlayersButtonProps {
  /** Rendered more prominently (larger CTA) when the screen has no players. */
  prominent?: boolean;
}

export function DiscoverPlayersButton({ prominent = false }: DiscoverPlayersButtonProps) {
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleDiscover() {
    setLoading(true);
    setError(null);
    setDetail(null);
    try {
      const engagementId = await resolveEngagementId();
      if (!engagementId) {
        setError("No active engagement found. Select or create one first.");
        return;
      }
      const res = await api.populatePlayers(engagementId);
      setDetail(
        res.detail ??
          "Discovering top players per segment in the background.",
      );
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to start player discovery.",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className={prominent ? "space-y-3" : "inline-flex flex-col items-start gap-2"}>
      <button
        type="button"
        onClick={handleDiscover}
        disabled={loading}
        className="inline-flex items-center gap-2 rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-brand/90 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {loading ? (
          <>
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white border-t-transparent" />
            Discovering players…
          </>
        ) : (
          <>
            <svg
              viewBox="0 0 24 24"
              className="h-4 w-4"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.7}
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <path d="M16 11a4 4 0 10-8 0" />
              <path d="M2 21a8 8 0 0120 0" />
              <path d="M20 8a3 3 0 11-6 0" />
              <path d="M22 21a6 6 0 00-9-5.2" />
            </svg>
            Discover players (AI)
          </>
        )}
      </button>

      {detail && (
        <div className="rounded-card border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
          <p className="font-medium">{detail}</p>
          <p className="mt-1 text-emerald-700">
            This runs in the background — refresh in a few minutes to see the
            discovered players per cell.
          </p>
        </div>
      )}

      {error && (
        <p className="text-sm text-red-600" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export default DiscoverPlayersButton;
