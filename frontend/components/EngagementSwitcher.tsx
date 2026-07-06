"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { Engagement } from "@/lib/types";

/**
 * Compact engagement switcher for the top of the left nav.
 *
 * The backend lives on a different origin than the frontend, so its
 * `Set-Cookie` never sticks to the frontend document. After activating an
 * engagement we therefore set the `engagement_id` cookie CLIENT-SIDE on the
 * frontend origin and do a FULL page reload so every server component
 * re-fetches scoped to the newly-selected engagement.
 */

/** Read the active engagement id from `document.cookie` (client only). */
function readCookieEngagementId(): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie
    .split(";")
    .map((c) => c.trim())
    .find((c) => c.startsWith("engagement_id="));
  if (!match) return null;
  const value = match.slice("engagement_id=".length);
  return value ? decodeURIComponent(value) : null;
}

/** Persist the active engagement on the FRONTEND origin, then hard-reload. */
function activateAndReload(id: string): void {
  document.cookie = `engagement_id=${id}; path=/; max-age=31536000; samesite=lax`;
  window.location.reload();
}

/** Remove the active engagement cookie on the FRONTEND origin. */
function clearEngagementCookie(): void {
  document.cookie = "engagement_id=; path=/; max-age=0; samesite=lax";
}

const chevron = (
  <svg
    viewBox="0 0 24 24"
    className="h-3.5 w-3.5 shrink-0 opacity-70"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden
  >
    <path d="M6 9l6 6 6-6" />
  </svg>
);

const plus = (
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
    <path d="M12 5v14M5 12h14" />
  </svg>
);

const check = (
  <svg
    viewBox="0 0 24 24"
    className="h-3.5 w-3.5 shrink-0 text-brand"
    fill="none"
    stroke="currentColor"
    strokeWidth={2.2}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden
  >
    <path d="M20 6L9 17l-5-5" />
  </svg>
);

const pencil = (
  <svg
    viewBox="0 0 24 24"
    className="h-3.5 w-3.5"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden
  >
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
  </svg>
);

const trash = (
  <svg
    viewBox="0 0 24 24"
    className="h-3.5 w-3.5"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden
  >
    <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M10 11v6M14 11v6" />
  </svg>
);

function DemoChip() {
  return (
    <span className="rounded bg-brand/25 px-1.5 py-0.5 text-2xs font-medium uppercase tracking-wide text-white/90">
      demo
    </span>
  );
}

export function EngagementSwitcher() {
  const router = useRouter();
  const [engagements, setEngagements] = useState<Engagement[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    setActiveId(readCookieEngagementId());
    api
      .listEngagements()
      .then((list) => {
        if (!cancelled) setEngagements(list);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Close the menu on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    function onPointer(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Render nothing until we have data (fail closed on error).
  if (failed || !engagements || engagements.length === 0) return null;

  // Resolve the active engagement: cookie match, else the demo, else first.
  const active =
    engagements.find((e) => e.engagement_id === activeId) ??
    engagements.find((e) => e.is_demo) ??
    engagements[0];
  const activeEngagementId = active.engagement_id;

  function select(engagement: Engagement) {
    if (busy) return;
    if (engagement.engagement_id === activeEngagementId) {
      setOpen(false);
      return;
    }
    setBusy(true);
    api
      .activateEngagement(engagement.engagement_id)
      .then(() => activateAndReload(engagement.engagement_id))
      .catch(() => {
        // Even if the server call hiccups, honor the client-side selection.
        activateAndReload(engagement.engagement_id);
      });
  }

  function archive(e: React.MouseEvent, engagement: Engagement) {
    e.stopPropagation();
    if (busy || engagement.is_demo) return;
    setBusy(true);
    api
      .archiveEngagement(engagement.engagement_id)
      .then(() => window.location.reload())
      .catch(() => setBusy(false));
  }

  function rename(e: React.MouseEvent, engagement: Engagement) {
    e.stopPropagation();
    if (busy || engagement.is_demo) return;
    const next = window.prompt("Rename engagement", engagement.name);
    if (next === null) return; // cancelled
    const trimmed = next.trim();
    if (!trimmed || trimmed === engagement.name) return;
    setBusy(true);
    api
      .renameEngagement(engagement.engagement_id, trimmed)
      .then(() => window.location.reload())
      .catch(() => setBusy(false));
  }

  function remove(e: React.MouseEvent, engagement: Engagement) {
    e.stopPropagation();
    if (busy || engagement.is_demo) return;
    if (
      !window.confirm(
        `Delete '${engagement.name}' and ALL its data? This cannot be undone.`,
      )
    ) {
      return;
    }
    setBusy(true);
    api
      .deleteEngagement(engagement.engagement_id)
      .then(() => {
        if (engagement.engagement_id === activeEngagementId) {
          // The active engagement was deleted: fall back to the demo (or the
          // first surviving engagement), else clear the cookie entirely.
          const survivors = (engagements ?? []).filter(
            (x) => x.engagement_id !== engagement.engagement_id,
          );
          const target =
            survivors.find((x) => x.is_demo) ?? survivors[0] ?? null;
          if (target) {
            activateAndReload(target.engagement_id);
          } else {
            clearEngagementCookie();
            window.location.reload();
          }
          return;
        }
        window.location.reload();
      })
      .catch(() => setBusy(false));
  }

  return (
    <div ref={rootRef} className="relative px-3 pt-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        aria-haspopup="menu"
        aria-expanded={open}
        className="focusable flex w-full items-center gap-2 rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-left text-sm text-white transition-colors hover:bg-white/10 disabled:opacity-60"
      >
        <div className="min-w-0 flex-1">
          <div className="text-2xs uppercase tracking-wide text-white/45">
            Engagement
          </div>
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{active.name}</span>
          </div>
        </div>
        {chevron}
      </button>

      {open ? (
        <div
          role="menu"
          className="absolute left-3 right-3 z-20 mt-1 overflow-hidden rounded-lg border border-white/10 bg-surface-inverse py-1 shadow-xl shadow-black/40"
        >
          <div className="max-h-72 overflow-y-auto py-0.5">
            {engagements.map((engagement) => {
              const isActive =
                engagement.engagement_id === activeEngagementId;
              return (
                <button
                  key={engagement.engagement_id}
                  type="button"
                  role="menuitem"
                  onClick={() => select(engagement)}
                  disabled={busy}
                  className={`group flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors disabled:opacity-60 ${
                    isActive
                      ? "bg-white/10 text-white"
                      : "text-white/80 hover:bg-white/5 hover:text-white"
                  }`}
                >
                  <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center">
                    {isActive ? check : null}
                  </span>
                  <span className="truncate">{engagement.name}</span>
                  <span className="ml-auto flex items-center gap-0.5">
                    {!engagement.is_demo ? (
                      <>
                        <span
                          role="button"
                          tabIndex={0}
                          aria-label={`Rename ${engagement.name}`}
                          title="Rename engagement"
                          onClick={(ev) => rename(ev, engagement)}
                          onKeyDown={(ev) => {
                            if (ev.key === "Enter" || ev.key === " ") {
                              rename(
                                ev as unknown as React.MouseEvent,
                                engagement,
                              );
                            }
                          }}
                          className="rounded p-0.5 text-white/40 opacity-0 transition-opacity hover:text-white group-hover:opacity-100 aria-disabled:pointer-events-none aria-disabled:opacity-30"
                          aria-disabled={busy}
                        >
                          {pencil}
                        </span>
                        <span
                          role="button"
                          tabIndex={0}
                          aria-label={`Archive ${engagement.name}`}
                          title="Archive engagement"
                          onClick={(ev) => archive(ev, engagement)}
                          onKeyDown={(ev) => {
                            if (ev.key === "Enter" || ev.key === " ") {
                              archive(
                                ev as unknown as React.MouseEvent,
                                engagement,
                              );
                            }
                          }}
                          className="rounded p-0.5 text-white/40 opacity-0 transition-opacity hover:text-white group-hover:opacity-100 aria-disabled:pointer-events-none aria-disabled:opacity-30"
                          aria-disabled={busy}
                        >
                          <svg
                            viewBox="0 0 24 24"
                            className="h-3.5 w-3.5"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth={2}
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            aria-hidden
                          >
                            <path d="M18 6L6 18M6 6l12 12" />
                          </svg>
                        </span>
                        <span
                          role="button"
                          tabIndex={0}
                          aria-label={`Delete ${engagement.name}`}
                          title="Delete engagement and all its data"
                          onClick={(ev) => remove(ev, engagement)}
                          onKeyDown={(ev) => {
                            if (ev.key === "Enter" || ev.key === " ") {
                              remove(
                                ev as unknown as React.MouseEvent,
                                engagement,
                              );
                            }
                          }}
                          className="rounded p-0.5 text-red-400/60 opacity-0 transition-opacity hover:text-red-400 group-hover:opacity-100 aria-disabled:pointer-events-none aria-disabled:opacity-30"
                          aria-disabled={busy}
                        >
                          {trash}
                        </span>
                      </>
                    ) : null}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="mt-0.5 border-t border-white/10 pt-0.5">
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                router.push("/brief");
              }}
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-white/80 transition-colors hover:bg-white/5 hover:text-white"
            >
              {plus}
              <span>New engagement</span>
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default EngagementSwitcher;
