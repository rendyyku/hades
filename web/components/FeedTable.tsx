import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { solscanTx } from "@/lib/evidence";
import { formatSol, relTime, shortAddr } from "@/lib/format";
import { num } from "@/lib/hlView";
import type { FeedItem, HlAlert } from "@/lib/live";
import { Address } from "./Address";
import { SideBadge } from "./AlertTable";
import { HeadLabel } from "./HeadLabel";
import { HlAddress } from "./HlAddress";
import { ScrollRegion } from "./ScrollRegion";

const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted whitespace-nowrap";
const TD = "px-3 py-3 align-middle whitespace-nowrap";
const LINK =
  "inline-flex min-h-8 cursor-pointer items-center gap-1 rounded font-mono text-fg underline decoration-border underline-offset-4 transition-colors duration-200 hover:decoration-fg";

/** Unsigned SOL amount: an alert's size is a magnitude, the side carries the direction. */
function sol(n: number | null): string {
  return n == null ? "—" : formatSol(Math.abs(n)).replace(/^\+/, "");
}

const sz = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 6 });

function hlEvent(a: HlAlert): string {
  const dir = typeof a.payload.direction === "string" ? ` ${a.payload.direction}` : "";
  return a.kind === "ladder" ? "ladder" : `${a.kind}${dir}`;
}

function hlSize(a: HlAlert): string {
  const s = num(a.payload.size);
  if (s == null) return "—";
  const pct = num(a.payload.change_pct);
  return `${sz.format(Math.abs(s))}${pct != null ? ` (${Math.round(pct * 100)}%)` : ""}`;
}

function HlRef({ a }: { a: HlAlert }) {
  if (a.kind === "ladder") {
    const from = typeof a.payload.from === "string" ? a.payload.from : "—";
    const to = typeof a.payload.to === "string" ? a.payload.to : "—";
    return (
      <Link href={`/arena/hyperliquid/${a.wallet}`} className={LINK}>
        {from} → {to}
      </Link>
    );
  }
  return (
    <Link href={`/arena/hyperliquid/${a.wallet}`} className={LINK}>
      paper
    </Link>
  );
}

type Props = {
  rows: FeedItem[];
  /** Epoch ms used for the relative times (ticked by LiveFeed). */
  now: number;
  caption: string;
  /** Rows whose key is NOT in this set get the slide-in (motion-safe only). */
  initialKeys?: ReadonlySet<string>;
};

/** Cerberus merged feed rows (every head). Hook-free; scrolls inside a focusable region. */
export function FeedTable({ rows, now, caption, initialKeys }: Props) {
  const at = new Date(now);
  return (
    <ScrollRegion label={`${caption} (scrollable)`} className="rounded-lg border border-border">
      <table className="w-full min-w-[880px] border-collapse text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="border-b border-border">
          <tr>
            <th scope="col" className={TH}>Time</th>
            <th scope="col" className={TH}>Head</th>
            <th scope="col" className={TH}>Wallet</th>
            <th scope="col" className={TH}>Event</th>
            <th scope="col" className={`${TH} text-right`}>Size</th>
            <th scope="col" className={TH}>Market</th>
            <th scope="col" className={TH}>Ref</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((i) => (
            <tr
              key={i.key}
              className={`border-b border-border transition-colors duration-200 last:border-b-0 hover:bg-white/[0.03] ${
                initialKeys && !initialKeys.has(i.key) ? "motion-safe:animate-[hades-slide-in_300ms_ease-out]" : ""
              }`}
            >
              <td className={`${TD} font-mono tabular-nums text-muted`}>
                {i.at ? (
                  <time dateTime={i.at} suppressHydrationWarning>
                    {relTime(i.at, at)}
                  </time>
                ) : (
                  "—"
                )}
              </td>
              <td className={TD}>
                <HeadLabel head={i.head} />
              </td>
              {i.head === "solana" ? (
                <>
                  <td className={TD}>
                    <Address value={i.alert.wallet} href={`/wallet/${i.alert.wallet}`} />
                  </td>
                  <td className={TD}>
                    <SideBadge side={i.alert.side} />
                  </td>
                  <td className={`${TD} text-right font-mono tabular-nums text-fg`}>{sol(i.alert.sol_amount)}</td>
                  <td className={TD}>
                    <Link href={`/token/${i.alert.mint}`} title={i.alert.mint} className={LINK}>
                      {i.alert.symbol || shortAddr(i.alert.mint)}
                    </Link>
                  </td>
                  <td className={TD}>
                    <a
                      href={solscanTx(i.alert.signature)}
                      target="_blank"
                      rel="noopener"
                      aria-label={`Transaction ${shortAddr(i.alert.signature)} on Solscan (opens in a new tab)`}
                      className={LINK}
                    >
                      {shortAddr(i.alert.signature)}
                      <ExternalLink aria-hidden="true" className="size-3 text-muted" />
                    </a>
                  </td>
                </>
              ) : (
                <>
                  <td className={TD}>
                    <HlAddress value={i.alert.wallet} href={`/hyperliquid/wallet/${i.alert.wallet}`} />
                  </td>
                  <td className={TD}>
                    <SideBadge side={hlEvent(i.alert)} />
                  </td>
                  <td className={`${TD} text-right font-mono tabular-nums text-fg`}>{hlSize(i.alert)}</td>
                  <td className={`${TD} font-mono text-fg`}>{i.alert.market_id ?? "—"}</td>
                  <td className={TD}>
                    <HlRef a={i.alert} />
                  </td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </ScrollRegion>
  );
}
