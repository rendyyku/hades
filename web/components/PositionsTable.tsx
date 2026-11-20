import { CircleCheck, CircleDot } from "lucide-react";
import Link from "next/link";
import type { PositionRow } from "@/lib/api";
import { formatSol, relTime, shortAddr } from "@/lib/format";
import { Pnl } from "./Pnl";
import { ScrollRegion } from "@/components/ScrollRegion";

const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted whitespace-nowrap";
const TD = "px-3 py-3 align-middle whitespace-nowrap";

const tokens = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 });

/** Unsigned SOL amount (cost / proceeds are magnitudes, not gains). */
function sol(n: number): string {
  return formatSol(Math.abs(n)).replace(/^\+/, "");
}

export function PositionsTable({ rows }: { rows: PositionRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">No SOL-quoted positions.</p>
    );
  }
  return (
    <ScrollRegion label="Positions (scrollable)" className="rounded-lg border border-border">
      <table className="w-full min-w-[820px] border-collapse text-sm">
        <caption className="sr-only">Positions by last activity</caption>
        <thead className="border-b border-border">
          <tr>
            <th scope="col" className={TH}>Token</th>
            <th scope="col" className={`${TH} text-right`}>Cost</th>
            <th scope="col" className={`${TH} text-right`}>Proceeds</th>
            <th scope="col" className={`${TH} text-right`}>Realized</th>
            <th scope="col" className={`${TH} text-right`}>Remaining</th>
            <th scope="col" className={TH}>Status</th>
            <th scope="col" className={`${TH} text-right`}>Last activity</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => {
            const last = p.last_sell_time ?? p.first_buy_time;
            return (
              <tr
                key={p.mint}
                className="border-b border-border transition-colors duration-200 last:border-b-0 hover:bg-white/[0.03]"
              >
                <td className={TD}>
                  <Link
                    href={`/token/${p.mint}`}
                    title={p.mint}
                    className="cursor-pointer rounded font-mono text-fg underline decoration-border underline-offset-4 transition-colors duration-200 hover:decoration-fg"
                  >
                    {p.symbol || shortAddr(p.mint)}
                  </Link>
                </td>
                <td className={`${TD} text-right font-mono tabular-nums`}>{sol(p.cost_sol)}</td>
                <td className={`${TD} text-right font-mono tabular-nums`}>{sol(p.proceeds_sol)}</td>
                <td className={`${TD} text-right font-mono tabular-nums`}>
                  <Pnl value={p.realized_pnl_sol} />
                </td>
                <td className={`${TD} text-right font-mono tabular-nums text-muted`}>
                  {tokens.format(p.remaining_tokens)}
                </td>
                <td className={TD}>
                  {p.closed ? (
                    <span className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 font-mono text-[11px] uppercase tracking-wide text-muted">
                      <CircleCheck aria-hidden="true" className="size-3" />
                      Closed
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 rounded border border-primary px-1.5 py-0.5 font-mono text-[11px] uppercase tracking-wide text-fg">
                      <CircleDot aria-hidden="true" className="size-3" />
                      Open
                    </span>
                  )}
                </td>
                <td className={`${TD} text-right font-mono tabular-nums text-muted`}>
                  {last ? <time dateTime={last}>{relTime(last)}</time> : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </ScrollRegion>
  );
}
