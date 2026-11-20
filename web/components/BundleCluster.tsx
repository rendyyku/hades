import type { Bundle } from "@/lib/api";
import { Address } from "./Address";

/** Tartarus bundle cluster: funder + the wallets it funded that bought in the same slot. */
export function BundleCluster({ bundle, index }: { bundle: Bundle; index: number }) {
  const headingId = `bundle-${index}`;
  return (
    <article
      aria-labelledby={headingId}
      className="flex flex-col gap-4 rounded-lg border border-dashed border-ember/60 bg-surface p-5"
    >
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 id={headingId} className="font-mono text-sm uppercase tracking-[0.14em] text-ember">
          Cluster {bundle.cluster}
        </h3>
        <span className="font-mono text-xs tabular-nums text-muted">
          {bundle.wallets.length} {bundle.wallets.length === 1 ? "wallet" : "wallets"}
        </span>
      </header>
      <dl className="grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-1 text-sm">
        <dt className="text-xs uppercase tracking-[0.14em] text-muted">Funder</dt>
        <dd>
          {bundle.funder ? (
            <Address value={bundle.funder} href={`/wallet/${bundle.funder}`} />
          ) : (
            <span className="font-mono text-muted">unknown</span>
          )}
        </dd>
      </dl>
      <ul aria-label={`Wallets in cluster ${bundle.cluster}`} className="flex flex-col gap-1">
        {bundle.wallets.map((w) => (
          <li key={w}>
            <Address value={w} href={`/wallet/${w}`} />
          </li>
        ))}
      </ul>
    </article>
  );
}
