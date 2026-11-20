import { ExternalLink } from "lucide-react";
import type { FundingHop } from "@/lib/api";
import { solscanTx } from "@/lib/evidence";
import { shortAddr } from "@/lib/format";
import { Address } from "./Address";

const nf = new Intl.NumberFormat("en-US");

export function TxLink({ sig }: { sig: string }) {
  return (
    <a
      href={solscanTx(sig)}
      target="_blank"
      rel="noopener"
      aria-label={`Transaction ${sig} on Solscan (opens in a new tab)`}
      className="inline-flex cursor-pointer items-center gap-1 rounded font-mono text-fg underline decoration-border underline-offset-4 transition-colors duration-200 hover:decoration-fg"
    >
      {shortAddr(sig)}
      <ExternalLink aria-hidden="true" className="size-3 text-muted" />
    </a>
  );
}

/** Styx: vertical funding trace, the wallet first, each hop funded by the next one. */
export function FundingChain({ chain }: { chain: FundingHop[] }) {
  if (chain.length === 0) {
    return <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">No funding data.</p>;
  }
  return (
    <ol className="flex flex-col gap-0 rounded-lg border border-border bg-surface p-5 sm:p-6">
      {chain.map((hop, i) => {
        const funder = chain[i + 1]?.address ?? null;
        return (
          <li key={`${hop.address}-${i}`} className="relative flex gap-4 pb-6 last:pb-0">
            {/* rail */}
            <div aria-hidden="true" className="flex flex-col items-center">
              <span className={`mt-2.5 size-2.5 rounded-full ${i === 0 ? "bg-ember" : "bg-muted"}`} />
              {i < chain.length - 1 ? <span className="mt-1 w-px flex-1 bg-border" /> : null}
            </div>
            <div className="flex min-w-0 flex-col gap-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs tabular-nums text-muted">{i === 0 ? "WALLET" : `HOP ${i}`}</span>
                <Address value={hop.address} href={`/wallet/${hop.address}`} />
              </div>
              <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted">
                <span>
                  ← funded by{" "}
                  <span className="font-mono text-fg">{funder ? shortAddr(funder) : "unknown funder"}</span>
                </span>
                <span aria-hidden="true">·</span>
                <span>
                  tx {hop.funding_signature ? <TxLink sig={hop.funding_signature} /> : <span className="font-mono">unknown</span>}
                </span>
                {hop.funding_slot != null ? (
                  <span className="font-mono tabular-nums">slot {nf.format(hop.funding_slot)}</span>
                ) : null}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
