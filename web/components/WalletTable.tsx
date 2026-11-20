import { Minus, TrendingDown, TrendingUp } from "lucide-react";
import type { WalletRow } from "@/lib/api";
import { formatPct, formatScore, formatSol } from "@/lib/format";
import { Address } from "./Address";

function Pnl({ value }: { value: number | null }) {
  if (value === null || value === 0) {
    return (
      <span className="inline-flex items-center justify-end gap-1.5 text-muted">
        <Minus aria-hidden="true" className="size-3.5" />
        {formatSol(value)}
      </span>
    );
  }
  const up = value > 0;
  const Icon = up ? TrendingUp : TrendingDown;
  return (
    <span className={`inline-flex items-center justify-end gap-1.5 ${up ? "text-gain" : "text-loss"}`}>
      <Icon aria-hidden="true" className="size-3.5" />
      {formatSol(value)}
    </span>
  );
}

const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted";
const TD = "px-3 py-3 align-middle";

export function WalletTable({ rows }: { rows: WalletRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
        No scored wallets yet. Run a backfill and <code className="font-mono">hades rescore</code>.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full min-w-[760px] border-collapse text-sm">
        <caption className="sr-only">Top wallets by score</caption>
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
              <td className={`${TD} font-mono tabular-nums text-muted`}>{i + 1}</td>
              <td className={TD}>
                <Address value={r.address} />
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
                    <li
                      key={t}
                      className="rounded border border-ember/40 px-1.5 py-0.5 font-mono text-[11px] uppercase tracking-wide text-ember"
                    >
                      {t}
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
