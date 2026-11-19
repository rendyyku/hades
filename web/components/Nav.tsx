import Link from "next/link";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/wallets", label: "Judgement" },
  { href: "/live", label: "Cerberus" },
  { href: "/watchlist", label: "Watchlist" },
] as const;

export function Nav() {
  return (
    <header className="sticky top-0 z-40 border-b border-border bg-bg/80 backdrop-blur-md">
      <nav
        aria-label="Primary"
        className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-4 sm:px-6 lg:px-8"
      >
        <Link
          href="/"
          className="flex shrink-0 cursor-pointer items-center gap-2 font-display text-xl font-normal uppercase tracking-[0.18em] text-fg"
        >
          <span aria-hidden="true" className="inline-block size-2 rounded-full bg-ember" />
          Hades
        </Link>
        <ul className="-mx-2 flex min-w-0 flex-1 items-center gap-1 overflow-x-auto sm:justify-end">
          {LINKS.map((l) => (
            <li key={l.href} className="shrink-0">
              <Link
                href={l.href}
                className="inline-flex min-h-11 cursor-pointer items-center rounded px-2 text-sm text-muted transition-colors duration-200 hover:text-fg"
              >
                {l.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </header>
  );
}
