import type { PaperPositionRow } from "@/lib/arena";
import { formatUsd } from "@/lib/format";

const TD = "px-3 py-2 align-middle whitespace-nowrap";
const TH = "px-3 py-2 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted whitespace-nowrap";
const qty = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 6 });
const x1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

/** Our open paper positions (the copies, at our size), as of the last paper run. */
export function PaperPositionsTable({ positions }: { positions: PaperPositionRow[] }) {
  if (positions.length === 0) {
    return <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">No open paper positions.</p>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">Open paper positions</caption>
        <thead className="border-b border-border">
          <tr>
            <th scope="col" className={TH}>Coin</th>
            <th scope="col" className={TH}>Side</th>
            <th scope="col" className={`${TH} text-right`}>Our size</th>
            <th scope="col" className={`${TH} text-right`}>Entry</th>
            <th scope="col" className={`${TH} text-right`}>Margin</th>
            <th scope="col" className={`${TH} text-right`}>Leverage</th>
            <th scope="col" className={`${TH} text-right`}>Leader size</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr key={p.positionId} className="border-b border-border last:border-b-0">
              <td className={`${TD} font-mono text-fg`}>{p.coin}</td>
              <td className={TD}>{p.direction}</td>
              <td className={`${TD} text-right font-mono tabular-nums`}>{p.qty != null ? qty.format(p.qty) : "—"}</td>
              <td className={`${TD} text-right font-mono tabular-nums`}>{formatUsd(p.entryPrice, { signed: false })}</td>
              <td className={`${TD} text-right font-mono tabular-nums`}>{formatUsd(p.margin, { signed: false })}</td>
              <td className={`${TD} text-right font-mono tabular-nums`}>{p.leverage != null ? `${x1.format(p.leverage)}x` : "—"}</td>
              <td className={`${TD} text-right font-mono tabular-nums text-muted`}>{p.leaderSize != null ? qty.format(p.leaderSize) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
