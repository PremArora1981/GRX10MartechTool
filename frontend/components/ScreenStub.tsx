import type { ReactNode } from "react";
import { PageHeader } from "./PageHeader";

/**
 * Placeholder body used by the un-built screens so the left-nav resolves during
 * parallel development. A screen agent replaces the entire page file; this
 * component is just scaffolding and may be deleted when all screens land.
 */
export function ScreenStub({
  eyebrow,
  title,
  description,
  owner,
  bullets = [],
}: {
  eyebrow: string;
  title: string;
  description: string;
  owner: string;
  bullets?: ReactNode[];
}) {
  return (
    <>
      <PageHeader eyebrow={eyebrow} title={title} description={description} />
      <div className="card border-dashed p-8 text-center">
        <div className="mx-auto max-w-md">
          <div className="eyebrow mb-2">Awaiting screen agent</div>
          <p className="text-sm text-ink-muted">
            This screen is owned by{" "}
            <span className="font-medium text-ink">{owner}</span>. The foundation
            shell, design system, API client, and auth are wired and ready to
            consume.
          </p>
          {bullets.length > 0 && (
            <ul className="mt-4 space-y-1 text-left text-sm text-ink-muted">
              {bullets.map((b, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-brand" aria-hidden>
                    •
                  </span>
                  <span>{b}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </>
  );
}

export default ScreenStub;
