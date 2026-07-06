"use client";

/**
 * SeedingProgress — dashboard progress banner for background market-cell seeding.
 *
 * On mount (and every ~15s) polls GET /engagements/{id}/status for the active
 * engagement. While `cells_planned > 0` it renders a small banner showing how
 * many cells have been sized so far. Once seeding completes (cells_planned === 0)
 * the banner hides itself and polling stops.
 *
 * The active engagement is resolved from api.currentEngagement(), falling back
 * to the `engagement_id` cookie.
 */

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

const POLL_INTERVAL_MS = 15_000;

type Status = {
  cells_total: number;
  cells_sized: number;
  cells_planned: number;
  players: number;
};

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

export function SeedingProgress() {
  const [status, setStatus] = useState<Status | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const engIdRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    function stopPolling() {
      if (timerRef.current !== null) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    }

    async function poll() {
      const engagementId =
        engIdRef.current ?? (await resolveEngagementId());
      if (!engagementId) return;
      engIdRef.current = engagementId;

      try {
        const s = await api.engagementStatus(engagementId);
        if (cancelled) return;
        setStatus({
          cells_total: s.cells_total,
          cells_sized: s.cells_sized,
          cells_planned: s.cells_planned,
          players: s.players,
        });
        // Seeding finished — stop polling.
        if (s.cells_planned === 0) stopPolling();
      } catch {
        // Transient/backend error — keep the last known state, retry next tick.
      }
    }

    // Initial read + interval.
    void poll();
    timerRef.current = setInterval(() => void poll(), POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      stopPolling();
    };
  }, []);

  // Hidden entirely until we know cells are still being planned.
  if (!status || status.cells_planned === 0) return null;

  return (
    <div
      className="card mb-6 flex items-center gap-3 border-brand/40 bg-surface-subtle p-4 text-sm text-ink-muted"
      role="status"
      aria-live="polite"
    >
      <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-brand border-t-transparent" />
      <p>
        <span className="font-medium text-ink">Populating market cells — </span>
        <span className="tnum">
          {status.cells_sized.toLocaleString()}/
          {status.cells_total.toLocaleString()}
        </span>{" "}
        sized
        {status.players > 0 && (
          <>
            {" · "}
            <span className="tnum">{status.players.toLocaleString()}</span>{" "}
            players
          </>
        )}
        . This runs in the background; refresh to update.
      </p>
    </div>
  );
}

export default SeedingProgress;
