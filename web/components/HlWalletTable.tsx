import { ScrollRegion } from "@/components/ScrollRegion";
import { formatScore, formatWinrate } from "@/lib/format";
import { num, type HlWalletRow } from "@/lib/hlView";
import { HlAddress } from "./HlAddress";
import { TagBadge } from "./TagBadge";
import { UsdPnl } from "./UsdPnl";

const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted";
const TD = "px-3 py-3 align-middle";
const nf = new Intl.NumberFormat("en-US");

type Props = {
  rows: HlWalletRow[];
  caption?: string;
};

/** Hyperliquid leaderboard. Winrate is always shown with its n (formatWinrate). */
export function HlWalletTable({ rows, caption = "Top Hyperliquid wallets" }: Props) {
  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
        <p>
          No scored Hyperliquid wallets yet. Run <code className="font-mono">hades-hl backfill</code> and <code className="font-mono">hades-hl rescore</code>.
        </p>
      </div>
    );
  }
  return (
    <ScrollRegion label={`${caption} (scrollable)`} className="rounded-lg border border-border">
      <table className="w-full min-w-[760px] border-collapse text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="border-b border-border">
          <tr>
            <th scope="col" className={`${TH} w-12`}>#</th>
            <th scope="col" className={TH}>Wallet</th>
            <th scope="col" className={`${TH} text-right`}>Score</th>
            <th scope="col" className={`${TH} text-right`}>Net PnL</th>
            <th scope="col" className={`${TH} text-right`}>Win rate</th>
            <th scope="col" className={`${TH} text-right`}>Trades</th>
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
                <HlAddress value={r.address} href={`/hyperliquid/wallet/${r.address}`} />
              </td>
              <td className={`${TD} text-right font-mono tabular-nums text-fg`}>{formatScore(r.score)}</td>
              <td className={`${TD} text-right font-mono tabular-nums`}>
                <UsdPnl value={num(r.net_pnl_usd)} />
              </td>
              <td className={`${TD} text-right font-mono tabular-nums`}>{formatWinrate(r.winrate, r.n)}</td>
              <td className={`${TD} text-right font-mono tabular-nums`}>{nf.format(r.trades)}</td>
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
    </ScrollRegion>
  );
}
