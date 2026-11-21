// Live open positions of a Hyperliquid wallet (Task 14, P64): typed reader of
// GET /api/wallets/{address}/positions, display formatters and the poll state of the
// wallet page. Pure (fetch injected): safe for server and client components.

import { relTime } from "./format";
import { num } from "./hlView";
import type { LiveBadgeState } from "./live";

/** The page polls the same-origin proxy this often; the head caches 5 s per address. */
export const POSITIONS_POLL_MS = 5000;
/** No successful poll for this long (a hidden or frozen tab) reads as stale. */
export const POSITIONS_STALE_MS = 3 * POSITIONS_POLL_MS;

export interface HlPosition {
  coin: string;
  side: "long" | "short";
  /** Absolute size in coin units. */
  size: number | null;
  entryPx: number | null;
  markPx: number | null;
  positionValue: number | null;
  unrealizedPnl: number | null;
  /** Return on the position's margin, in percent. */
  roePct: number | null;
  leverage: number | null;
  /** "cross" | "isolated". */
  leverageType: string;
  liquidationPx: number | null;
  marginUsed: number | null;
}

export interface HlPositions {
  address: string;
  accountValue: number | null;
  totalUnrealizedPnl: number | null;
  positions: HlPosition[];
  /** ISO time of the state on Hyperliquid. */
  asOf: string | null;
  /** Which dex the list covers (the head says "main perp dex"). */
  scope: string | null;
}

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

const str = (v: unknown) => (typeof v === "string" ? v : null);

function readPosition(v: unknown): HlPosition | null {
  const p = obj(v);
  if (!p || typeof p.coin !== "string" || (p.side !== "long" && p.side !== "short")) return null;
  const lev = obj(p.leverage) ?? {};
  return {
    coin: p.coin,
    side: p.side,
    size: num(p.size),
    entryPx: num(p.entry_px),
    markPx: num(p.mark_px),
    positionValue: num(p.position_value),
    unrealizedPnl: num(p.unrealized_pnl),
    roePct: num(p.roe_pct),
    leverage: num(lev.value),
    leverageType: str(lev.type) ?? "",
    liquidationPx: num(p.liquidation_px),
    marginUsed: num(p.margin_used),
  };
}

/** The head's WalletPositions (decimals as JSON strings); null if it is not that shape. */
export function readPositions(v: unknown): HlPositions | null {
  const o = obj(v);
  if (!o || typeof o.address !== "string" || !Array.isArray(o.positions)) return null;
  return {
    address: o.address,
    accountValue: num(o.account_value),
    totalUnrealizedPnl: num(o.total_unrealized_pnl),
    positions: o.positions.map(readPosition).filter((p): p is HlPosition => p !== null),
    asOf: str(o.as_of),
    scope: str(o.scope),
  };
}

const DASH = "—";
const MINUS = "\u2212";
const sig6 = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 6 });
const lev1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

/** A price with up to 6 significant digits: `$60,387.8`, `$0.934985`. */
export function formatPx(n: number | null | undefined): string {
  return typeof n === "number" && Number.isFinite(n) ? `$${sig6.format(n)}` : DASH;
}

/** A size in coin units, up to 6 significant digits. */
export function formatQty(n: number | null | undefined): string {
  return typeof n === "number" && Number.isFinite(n) ? sig6.format(n) : DASH;
}

/** `20x isolated`, `3x cross`. */
export function leverageLabel(value: number | null, type: string): string {
  return `${value != null ? `${lev1.format(value)}x` : DASH} ${type}`.trim();
}

/** Signed percent with two decimals: `+2.33%`, `−1.36%` (U+2212). */
export function formatSignedPct(pct: number | null | undefined): string {
  if (typeof pct !== "number" || !Number.isFinite(pct)) return DASH;
  const body = `${Math.abs(pct).toFixed(2)}%`;
  return pct > 0 ? `+${body}` : pct < 0 ? `${MINUS}${body}` : body;
}

// -- poll state ---------------------------------------------------------------------------

export interface PositionsFeed {
  /** The last positions we loaded (kept on screen when a poll fails). */
  data: HlPositions | null;
  /** Epoch ms of the last successful load. */
  lastOkAt: number | null;
  /** The latest poll failed (head or Hyperliquid unreachable). */
  failed: boolean;
}

/** Fold one poll result (null = failed) into the feed. Never drops the last data. */
export function applyPoll(prev: PositionsFeed, result: HlPositions | null, now: number): PositionsFeed {
  if (result === null) return prev.failed ? prev : { ...prev, failed: true };
  return { data: result, lastOkAt: now, failed: false };
}

/** LIVE while the latest poll succeeded and is recent; else STALE since the last good load. */
export function positionsBadge(feed: PositionsFeed, now: number): LiveBadgeState {
  if (feed.lastOkAt == null) return { label: "STALE", since: "never" };
  if (!feed.failed && now - feed.lastOkAt < POSITIONS_STALE_MS) return { label: "LIVE" };
  return { label: "STALE", since: relTime(new Date(feed.lastOkAt).toISOString(), new Date(now)) };
}

/** One poll through the same-origin proxy (the head key stays on the server); null on any failure. */
export async function fetchPositions(address: string, fetchImpl: typeof fetch = fetch): Promise<HlPositions | null> {
  try {
    const res = await fetchImpl(`/api/heads/hyperliquid/wallets/${address}/positions`, { cache: "no-store" });
    if (!res.ok) return null;
    return readPositions(await res.json());
  } catch {
    return null;
  }
}
