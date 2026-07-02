import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-center">
      <div className="eyebrow">404</div>
      <h1 className="mt-1 text-2xl font-semibold text-ink">Page not found</h1>
      <p className="mt-2 max-w-sm text-sm text-ink-muted">
        That screen does not exist in the GRX10 Market Research Tool.
      </p>
      <Link
        href="/"
        className="focusable mt-5 rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand-700"
      >
        Back to dashboard
      </Link>
    </div>
  );
}
