import { formatUsd } from "@/lib/format";
import {
  formatPx,
  formatQty,
  formatSignedPct,
  leverageLabel,
  positionsBadge,
  type HlPosition,
  type PositionsFeed,
} from "@/lib/hlPositions";
import { BadgeView } from "./BadgeView";
import { UsdPnl } from "./UsdPnl";

const TD = "px-3 py-2 align-middle whitespace-nowrap";
const TH = "px-3 py-2 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted whitespace-nowrap";
const NUM = `${TD} text-right font-mono tabular-nums`;
const DT = "text-xs uppercase tracking-[0.14em] text-muted";
const DD = "font-mono tabular-nums text-fg";

/** Long / Short as text with a colour hint (never colour alone). */
function Side({ side }: { side: HlPosition["side"] }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[11px] uppercase tracking-wide ${
        side === "long" ? "border-gain/60 text-gain" : "border-loss/60 text-loss"
      }`}
    >
      {side === "long" ? "Long" : "Short"}
    </span>
  );
}

/** ROE in percent, coloured like the PnL it belongs to. */
function Roe({ pct }: { pct: number | null }) {
  const tone = pct == null || pct === 0 ? "text-muted" : pct > 0 ? "text-gain" : "text-loss";
  return <span className={tone}>{formatSignedPct(pct)}</span>;
}

function Table({ rows }: { rows: HlPosition[] }) {
  return (
    <div className="hidden sm:block overflow-x-auto rounded-lg border border-border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">Open positions, largest position value first</caption>
        <thead className="border-b border-border">
          <tr>
            <th scope="col" className={TH}>Coin</th>
            <th scope="col" className={TH}>Side</th>
            <th scope="col" className={`${TH} text-right`}>Size</th>
            <th scope="col" className={`${TH} text-right`}>Entry</th>
            <th scope="col" className={`${TH} text-right`}>Mark</th>
            <th scope="col" className={`${TH} text-right`}>Value</th>
            <th scope="col" className={`${TH} text-right`}>Unrealized PnL</th>
            <th scope="col" className={`${TH} text-right`}>ROE</th>
            <th scope="col" className={TH}>Leverage</th>
            <th scope="col" className={`${TH} text-right`}>Liq. price</th>
            <th scope="col" className={`${TH} text-right`}>Margin</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.coin} className="border-b border-border last:border-b-0">
              <td className={`${TD} font-mono text-fg`}>{p.coin}</td>
              <td className={TD}>
                <Side side={p.side} />
              </td>
              <td className={NUM}>{formatQty(p.size)}</td>
              <td className={NUM}>{formatPx(p.entryPx)}</td>
              <td className={NUM}>{formatPx(p.markPx)}</td>
              <td className={NUM}>{formatUsd(p.positionValue, { signed: false })}</td>
              <td className={NUM}>
                <UsdPnl value={p.unrealizedPnl} />
              </td>
              <td className={NUM}>
                <Roe pct={p.roePct} />
              </td>
              <td className={`${TD} font-mono text-xs text-muted`}>{leverageLabel(p.leverage, p.leverageType)}</td>
              <td className={NUM}>{formatPx(p.liquidationPx)}</td>
              <td className={`${NUM} text-muted`}>{formatUsd(p.marginUsed, { signed: false })}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Cards({ rows }: { rows: HlPosition[] }) {
  return (
    <ul aria-label="Open positions" className="flex flex-col gap-3 sm:hidden">
      {rows.map((p) => (
        <li key={p.coin} className="rounded-lg border border-border bg-surface p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <span className="font-mono text-base text-fg">{p.coin}</span>
            <Side side={p.side} />
            <span className="font-mono text-xs text-muted">{leverageLabel(p.leverage, p.leverageType)}</span>
            <span className="ml-auto font-mono text-sm tabular-nums">
              <UsdPnl value={p.unrealizedPnl} />
            </span>
          </div>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <div>
              <dt className={DT}>Size</dt>
              <dd className={DD}>{formatQty(p.size)}</dd>
            </div>
            <div>
              <dt className={DT}>Value</dt>
              <dd className={DD}>{formatUsd(p.positionValue, { signed: false })}</dd>
            </div>
            <div>
              <dt className={DT}>Entry</dt>
              <dd className={DD}>{formatPx(p.entryPx)}</dd>
            </div>
            <div>
              <dt className={DT}>Mark</dt>
              <dd className={DD}>{formatPx(p.markPx)}</dd>
            </div>
            <div>
              <dt className={DT}>ROE</dt>
              <dd className={DD}>
                <Roe pct={p.roePct} />
              </dd>
            </div>
            <div>
              <dt className={DT}>Liq. price</dt>
              <dd className={DD}>{formatPx(p.liquidationPx)}</dd>
            </div>
            <div>
              <dt className={DT}>Margin</dt>
              <dd className={DD}>{formatUsd(p.marginUsed, { signed: false })}</dd>
            </div>
          </dl>
        </li>
      ))}
    </ul>
  );
}

/**
 * The wallet's live open positions on Hyperliquid (P64): header with the total
 * unrealized PnL and the account value, a LIVE / STALE badge, then a table (cards on
 * mobile). Presentational and server-safe; OpenPositions is the polling wrapper.
 * A failed poll keeps the last data on screen and says it is stale.
 */
export function OpenPositionsView({ feed, now }: { feed: PositionsFeed; now: number }) {
  const badge = positionsBadge(feed, now);
  const d = feed.data;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-x-10 gap-y-4">
        {d ? (
          <dl className="flex flex-wrap gap-x-10 gap-y-4">
            <div>
              <dt className={DT}>Total unrealized PnL</dt>
              <dd className="mt-1 font-mono text-2xl tabular-nums sm:text-3xl">
                <UsdPnl value={d.totalUnrealizedPnl} align="start" />
              </dd>
            </div>
            <div>
              <dt className={DT}>Account value</dt>
              <dd className="mt-1 font-mono text-2xl tabular-nums text-fg sm:text-3xl">
                {formatUsd(d.accountValue, { signed: false })}
              </dd>
            </div>
          </dl>
        ) : null}
        <div className="ml-auto">
          <BadgeView state={badge} />
        </div>
      </div>

      <p role="status" aria-live="polite" className={feed.failed && d ? "text-sm text-loss" : "sr-only"}>
        {feed.failed && d ? "Could not refresh · showing the last positions we loaded. Retrying every 5 s." : ""}
      </p>

      {!d ? (
        <p role="alert" className="rounded-lg border border-loss/40 bg-loss/10 p-5 text-sm text-fg">
          Open positions are not available: the Hyperliquid head or the Hyperliquid API did not answer. Retrying every
          5 s.
        </p>
      ) : d.positions.length === 0 ? (
        <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">No open positions.</p>
      ) : (
        <>
          <Table rows={d.positions} />
          <Cards rows={d.positions} />
        </>
      )}

      {d ? (
        <p className="text-xs text-muted">
          Read from Hyperliquid every 5 s
          {d.asOf ? (
            <>
              {" "}
              · state of <time dateTime={d.asOf}>{d.asOf.replace("T", " ").slice(0, 19)} UTC</time>
            </>
          ) : null}
          . Covers the {d.scope ?? "main perp dex"}; positions on HIP-3 dexes are not listed. Mark is the price
          Hyperliquid valued the position at.
        </p>
      ) : null}
    </div>
  );
}
