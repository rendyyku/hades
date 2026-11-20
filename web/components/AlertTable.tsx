import { ArrowDownRight, ArrowUpRight, ExternalLink } from "lucide-react";
import Link from "next/link";
import type { AlertRow } from "@/lib/api";
import { solscanTx } from "@/lib/evidence";
import { formatSol, relTime, shortAddr } from "@/lib/format";
import { Address } from "./Address";

const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted whitespace-nowrap";
const TD = "px-3 py-3 align-middle whitespace-nowrap";

/** Unsigned SOL amount: an alert's size is a magnitude, the side carries the direction. */
function sol(n: number | null): string {
  return n == null ? "—" : formatSol(Math.abs(n)).replace(/^\+/, "");
}

export function SideBadge({ side }: { side: string }) {
  const buy = side.toLowerCase() === "buy";
  const sell = side.toLowerCase() === "sell";
  const Icon = buy ? ArrowUpRight : ArrowDownRight;
  const cls = buy ? "border-gain/60 text-gain" : sell ? "border-loss/60 text-loss" : "border-border text-muted";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[11px] uppercase tracking-wide ${cls}`}
    >
      {buy || sell ? <Icon aria-hidden="true" className="size-3" /> : null}
      {side.toUpperCase()}
    </span>
  );
}

type Props = {
  rows: AlertRow[];
  /** Epoch ms used for the relative times (ticked by LiveFeed). */
  now: number;
  caption: string;
  /** Rows with an id above this get the slide-in (motion-safe only). */
  animateAbove?: number;
};

/**
 * Cerberus alert rows. Hook-free so the Overview can render it on the server and
 * LiveFeed on the client. Scrolls horizontally inside a focusable named region.
 */
export function AlertTable({ rows, now, caption, animateAbove }: Props) {
  const at = new Date(now);
  return (
    <div
      role="region"
      aria-label={`${caption} (scrollable)`}
      tabIndex={0}
      className="overflow-x-auto rounded-lg border border-border"
    >
      <table className="w-full min-w-[760px] border-collapse text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="border-b border-border">
          <tr>
            <th scope="col" className={TH}>Time</th>
            <th scope="col" className={TH}>Wallet</th>
            <th scope="col" className={TH}>Side</th>
            <th scope="col" className={`${TH} text-right`}>SOL</th>
            <th scope="col" className={TH}>Token</th>
            <th scope="col" className={TH}>Tx</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <tr
              key={a.id}
              className={`border-b border-border transition-colors duration-200 last:border-b-0 hover:bg-white/[0.03] ${
                animateAbove != null && a.id > animateAbove ? "motion-safe:animate-[hades-slide-in_300ms_ease-out]" : ""
              }`}
            >
              <td className={`${TD} font-mono tabular-nums text-muted`}>
                {a.block_time ? (
                  <time dateTime={a.block_time} suppressHydrationWarning>
                    {relTime(a.block_time, at)}
                  </time>
                ) : (
                  "—"
                )}
              </td>
              <td className={TD}>
                <Address value={a.wallet} href={`/wallet/${a.wallet}`} />
              </td>
              <td className={TD}>
                <SideBadge side={a.side} />
              </td>
              <td className={`${TD} text-right font-mono tabular-nums text-fg`}>{sol(a.sol_amount)}</td>
              <td className={TD}>
                <Link
                  href={`/token/${a.mint}`}
                  title={a.mint}
                  className="cursor-pointer rounded font-mono text-fg underline decoration-border underline-offset-4 transition-colors duration-200 hover:decoration-fg"
                >
                  {a.symbol || shortAddr(a.mint)}
                </Link>
              </td>
              <td className={TD}>
                <a
                  href={solscanTx(a.signature)}
                  target="_blank"
                  rel="noopener"
                  aria-label={`Transaction ${shortAddr(a.signature)} on Solscan (opens in a new tab)`}
                  className="inline-flex min-h-8 cursor-pointer items-center gap-1 rounded font-mono text-fg underline decoration-border underline-offset-4 transition-colors duration-200 hover:decoration-fg"
                >
                  {shortAddr(a.signature)}
                  <ExternalLink aria-hidden="true" className="size-3 text-muted" />
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
