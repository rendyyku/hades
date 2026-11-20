import type { ReactNode } from "react";
import type { WalletRow } from "@/lib/api";
import { formatPct, formatScore } from "@/lib/format";
import { Address } from "./Address";
import { Pnl } from "./Pnl";
import { TagBadge } from "./TagBadge";

const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted";
const TD = "px-3 py-3 align-middle";

type Props = {
  rows: WalletRow[];
  /** Added to the 1-based row index (pagination). */
  rankOffset?: number;
  caption?: string;
  empty?: ReactNode;
};

export function WalletTable({ rows, rankOffset = 0, caption = "Top wallets by score", empty }: Props) {
  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
        {empty ?? (
          <p>
            No scored wallets yet. Run a backfill and <code className="font-mono">hades rescore</code>.
          </p>
        )}
      </div>
    );
  }
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
            <th scope="col" className={`${TH} w-12`}>#</th>
            <th scope="col" className={TH}>Wallet</th>
            <th scope="col" className={`${TH} text-right`}>Score</th>
            <th scope="col" className={`${TH} text-right`}>Total PnL</th>
            <th scope="col" className={`${TH} text-right`}>Win rate</th>
            <th scope="col" className={`${TH} text-right`}>Closed</th>
            <th scope="col" className={TH}>Tags</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr
              key={r.address}
              className="border-b border-border transition-colors duration-200 last:border-b-0 hover:bg-white/[0.03]"
            >
              <td className={`${TD} font-mono tabular-nums text-muted`}>{rankOffset + i + 1}</td>
              <td className={TD}>
                <Address value={r.address} href={`/wallet/${r.address}`} />
              </td>
              <td className={`${TD} text-right font-mono tabular-nums text-fg`}>{formatScore(r.score)}</td>
              <td className={`${TD} text-right font-mono tabular-nums`}>
                <Pnl value={r.total_pnl_sol} />
              </td>
              <td className={`${TD} text-right font-mono tabular-nums`}>{formatPct(r.win_rate)}</td>
              <td className={`${TD} text-right font-mono tabular-nums`}>{r.tokens_closed ?? "—"}</td>
              <td className={TD}>
                <ul className="flex flex-wrap gap-1.5">
                  {r.tags.length === 0 ? <li className="text-muted">—</li> : null}
                  {r.tags.map((t) => (
                    <li key={t}>
                      <TagBadge tag={t} />
                    </li>
                  ))}
                </ul>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
