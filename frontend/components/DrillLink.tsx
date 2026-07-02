import Link from "next/link";
import type { ComponentProps, ReactNode } from "react";

/**
 * DrillLink — the canonical "drill down / drill to source" affordance that powers
 * the two-click audit chain (cell -> estimate -> source -> raw payload).
 *
 * Two modes:
 *   - internal (default): a Next.js <Link> to another screen.
 *   - external: opens a source URL in a new tab with an ↗ indicator (used by the
 *     Cell Detail "Sources" rows and the Reports clickable-sources requirement).
 */

type BaseProps = {
  children: ReactNode;
  /** Visual emphasis. `muted` for inline table cells, `primary` for CTAs. */
  variant?: "primary" | "muted";
  className?: string;
};

type InternalProps = BaseProps & {
  href: ComponentProps<typeof Link>["href"];
  external?: false;
  prefetch?: boolean;
};

type ExternalProps = BaseProps & {
  href: string;
  external: true;
};

export type DrillLinkProps = InternalProps | ExternalProps;

const VARIANT: Record<NonNullable<BaseProps["variant"]>, string> = {
  primary: "text-brand hover:text-brand-700 font-medium",
  muted: "text-ink-muted hover:text-ink underline-offset-2 hover:underline",
};

export function DrillLink(props: DrillLinkProps) {
  const { children, variant = "primary", className = "" } = props;
  const cls = `focusable inline-flex items-center gap-1 rounded ${VARIANT[variant]} ${className}`;

  if (props.external) {
    return (
      <a
        href={props.href}
        target="_blank"
        rel="noopener noreferrer"
        className={cls}
      >
        {children}
        <span aria-hidden className="text-[0.85em] opacity-70">
          ↗
        </span>
      </a>
    );
  }

  return (
    <Link href={props.href} prefetch={props.prefetch} className={cls}>
      {children}
      <span aria-hidden className="text-[0.85em] opacity-70">
        →
      </span>
    </Link>
  );
}

export default DrillLink;
