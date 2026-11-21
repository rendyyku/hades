import Link from "next/link";

/** Spec 10 order. Polymarket has no head yet: shown, but disabled (no page, no link). */
export const NAV_LINKS: readonly { href: string | null; label: string }[] = [
  { href: "/", label: "Overview" },
  { href: "/wallets", label: "Solana" },
  { href: "/hyperliquid", label: "Hyperliquid" },
  { href: null, label: "Polymarket" },
  { href: "/arena", label: "Paper Arena" },
  { href: "/live", label: "Cerberus" },
  { href: "/watchlist", label: "Watchlist" },
];

/**
 * Below 640px the links wrap onto a second row under the wordmark (no horizontal
 * scrollbar); from sm up they share one 56px row. Targets stay >= 44px tall.
 */
export function Nav() {
  return (
    <header className="sticky top-0 z-40 border-b border-border bg-bg/80 backdrop-blur-md">
      <nav
        aria-label="Primary"
        className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 px-4 py-1 sm:h-14 sm:flex-nowrap sm:px-6 sm:py-0 lg:px-8"
      >
        <Link
          href="/"
          className="flex min-h-11 shrink-0 cursor-pointer items-center gap-2 font-display text-xl font-normal uppercase tracking-[0.18em] text-fg"
        >
          <span aria-hidden="true" className="inline-block size-2 rounded-full bg-ember" />
          HADES
        </Link>
        <ul className="-mx-2 flex w-[calc(100%+1rem)] flex-wrap items-center justify-between gap-x-1 sm:w-auto sm:flex-1 sm:justify-end">
          {NAV_LINKS.map((l) => (
            <li key={l.label}>
              {l.href ? (
                <Link
                  href={l.href}
                  className="inline-flex min-h-11 cursor-pointer items-center rounded px-2 text-sm text-muted transition-colors duration-200 hover:text-fg"
                >
                  {l.label}
                </Link>
              ) : (
                <span
                  aria-disabled="true"
                  title="Polymarket head: coming soon"
                  className="inline-flex min-h-11 cursor-not-allowed items-center rounded px-2 text-sm text-muted/60"
                >
                  {l.label}
                  <span className="sr-only"> (coming soon)</span>
                </span>
              )}
            </li>
          ))}
        </ul>
      </nav>
    </header>
  );
}
