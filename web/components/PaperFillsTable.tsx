import { ExternalLink } from "lucide-react";
import { PAPER_FILLS_KEEP, type PaperFillRow } from "@/lib/arena";
import { formatUsd, shortAddr } from "@/lib/format";
import { hlTxUrl } from "@/lib/hlView";
import { UsdPnl } from "./UsdPnl";

const TD = "px-3 py-2 align-middle whitespace-nowrap";
const TH = "px-3 py-2 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted whitespace-nowrap";
const TEXT_LINK =
  "inline-flex cursor-pointer items-center gap-1 rounded font-mono text-fg underline decoration-border underline-offset-4 transition-colors duration-200 hover:decoration-fg";
const size = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 6 });

/** Where the row comes from: the leader's transaction, or our own synthetic event. */
function Source({ f }: { f: PaperFillRow }) {
  if (f.source === "liquidation") return <span className="text-loss">Paper liquidation</span>;
  if (f.source === "reconcile") return <span className="text-muted">Reconcile close</span>;
  const url = hlTxUrl(f.hash);
  return url ? (
    <a href={url} target="_blank" rel="noopener noreferrer" title={f.ref} className={TEXT_LINK}>
      {shortAddr(f.hash)}
      <ExternalLink aria-hidden="true" className="size-3.5" />
    </a>
  ) : (
    <span title={f.ref} className="font-mono text-muted">
      {f.ref.length > 22 ? `${f.ref.slice(0, 22)}…` : f.ref}
    </span>
  );
}

/** P69: a late fill of the order that opened the position. No copy trade, so no numbers. */
const ADJUST_LABEL = "Leader order completed (size adjust, no trade)";
const ADJUST_HINT =
  "A late fill of the order that opened this position. Our copy does not trade: its ratio follows the leader's new size.";

/**
 * The paper fills behind the paper PnL (spec 1: every claim traceable): what we
 * copied, at which modelled price, and which leader events we missed.
 */
export function PaperFillsTable({ fills, missedCount }: { fills: PaperFillRow[]; missedCount: number }) {
  if (fills.length === 0) {
    return (
      <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
        No paper fills yet: the leader has not traded since the follow started.
      </p>
    );
  }
  return (
    <>
      <p className="mb-4 text-sm text-muted">
        {fills.length >= PAPER_FILLS_KEEP ? `Latest ${PAPER_FILLS_KEEP} events` : `${fills.length} events`}, newest first.{" "}
        {missedCount} missed in total. Prices are our modelled copy prices, never the leader&apos;s.
      </p>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">Paper fills, newest first</caption>
          <thead className="border-b border-border">
            <tr>
              <th scope="col" className={TH}>When</th>
              <th scope="col" className={TH}>Coin</th>
              <th scope="col" className={TH}>Action</th>
              <th scope="col" className={`${TH} text-right`}>Our size</th>
              <th scope="col" className={`${TH} text-right`}>Our price</th>
              <th scope="col" className={`${TH} text-right`}>Fee</th>
              <th scope="col" className={`${TH} text-right`}>Realized PnL</th>
              <th scope="col" className={TH}>Source</th>
            </tr>
          </thead>
          <tbody>
            {fills.map((f, i) => (
              <tr key={`${f.ref}-${i}`} className="border-b border-border last:border-b-0">
                <td className={`${TD} font-mono text-xs text-muted`}>
                  {f.at ? <time dateTime={f.at}>{f.at.replace("T", " ").slice(0, 19)} UTC</time> : "—"}
                </td>
                <td className={`${TD} font-mono text-fg`}>{f.coin}</td>
                {f.side === "adjust" ? (
                  <td colSpan={5} title={ADJUST_HINT} className={`${TD} text-muted`}>
                    {ADJUST_LABEL}
                  </td>
                ) : (
                  <td className={TD}>
                    {f.side} {f.direction}
                    {f.missed ? (
                      <span className="ml-2 inline-flex items-center rounded border border-loss/50 px-1.5 py-0.5 font-mono text-[11px] uppercase tracking-wide text-loss">
                        Missed
                      </span>
                    ) : null}
                  </td>
                )}
                {f.side === "adjust" ? null : f.missed ? (
                  <td colSpan={4} className={`${TD} text-right text-muted`}>
                    not copied
                  </td>
                ) : (
                  <>
                    <td className={`${TD} text-right font-mono tabular-nums`}>{f.size != null ? size.format(f.size) : "—"}</td>
                    <td className={`${TD} text-right font-mono tabular-nums`}>{formatUsd(f.price, { signed: false })}</td>
                    <td className={`${TD} text-right font-mono tabular-nums text-muted`}>{formatUsd(f.fee, { signed: false })}</td>
                    <td className={`${TD} text-right font-mono tabular-nums`}>
                      <UsdPnl value={f.realizedPnl} />
                    </td>
                  </>
                )}
                <td className={TD}>
                  <Source f={f} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
