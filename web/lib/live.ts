// Cerberus live-feed state helpers. Pure: safe for server and client components.

import type { AlertRow } from "./api";
import { relTime } from "./format";
import { isHeadId, type HeadId } from "./heads";

/** LIVE only while the SSE is connected and the last event (alert or ping) is < 60 s old. */
export const LIVE_WINDOW_MS = 60_000;
export const FEED_CAP = 200;

export type LiveBadgeState = { label: "LIVE" } | { label: "STALE"; since: string };

export function liveBadge(connected: boolean, lastEventAt: number | null, now: number): LiveBadgeState {
  if (connected && lastEventAt != null && now - lastEventAt < LIVE_WINDOW_MS) return { label: "LIVE" };
  if (lastEventAt == null) return { label: "STALE", since: "never" };
  return { label: "STALE", since: relTime(new Date(lastEventAt).toISOString(), new Date(now)) };
}

/** Parse an SSE `alert` payload; null if it is not valid JSON or not AlertRow-shaped. */
export function parseAlertEvent(data: string): AlertRow | null {
  let v: unknown;
  try {
    v = JSON.parse(data);
  } catch {
    return null;
  }
  return toAlertRow(v);
}

/** Validate one decoded value as an AlertRow (null if it is not AlertRow-shaped). */
function toAlertRow(v: unknown): AlertRow | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  const str = (k: string) => typeof o[k] === "string";
  if (typeof o.id !== "number" || !Number.isFinite(o.id)) return null;
  if (!str("wallet") || !str("mint") || !str("side") || !str("signature")) return null;
  return {
    id: o.id,
    wallet: o.wallet as string,
    mint: o.mint as string,
    symbol: typeof o.symbol === "string" ? o.symbol : null,
    side: o.side as string,
    sol_amount: typeof o.sol_amount === "number" ? o.sol_amount : null,
    signature: o.signature as string,
    block_time: typeof o.block_time === "string" ? o.block_time : null,
  };
}

/** Dedupe by id (incoming wins), newest (highest id) first, capped. Never mutates its inputs. */
export function mergeAlerts(existing: AlertRow[], incoming: AlertRow[], cap: number = FEED_CAP): AlertRow[] {
  const byId = new Map<number, AlertRow>();
  for (const a of existing) byId.set(a.id, a);
  for (const a of incoming) byId.set(a.id, a);
  return [...byId.values()].sort((a, b) => b.id - a.id).slice(0, Math.max(0, cap));
}

/** Parse a decoded /api/alerts JSON body: keeps the AlertRow-shaped items, [] for non-arrays. */
export function parseAlertList(data: unknown): AlertRow[] {
  if (!Array.isArray(data)) return [];
  return data.map(toAlertRow).filter((a): a is AlertRow => a !== null);
}

/** "N new" for alerts received while paused; "cap+ new" once more arrived than the buffer keeps. */
export function pausedNewLabel(received: number, cap: number = FEED_CAP): string {
  return received > cap ? `${cap}+ new` : `${received} new`;
}

/** Backfill missed alerts on every (re)open except the very first one (the server render covers it). */
export function shouldBackfill(previousOpens: number): boolean {
  return previousOpens > 0;
}

// -- multi-head Cerberus feed (the /api/stream fan-in tags every event with `head`) ------

/** core/hades_core/contract.py Alert (Hyperliquid head). */
export interface HlAlert {
  id: number;
  kind: string;
  wallet: string;
  market_id: string | null;
  payload: Record<string, unknown>;
  created_at: string | null;
}

export type FeedItem =
  | { head: "solana"; key: string; id: number; at: string | null; alert: AlertRow }
  | { head: "hyperliquid"; key: string; id: number; at: string | null; alert: HlAlert };

export type FeedFilter = HeadId | "all";

function toHlAlert(v: unknown): HlAlert | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  if (typeof o.id !== "number" || !Number.isFinite(o.id)) return null;
  if (typeof o.kind !== "string" || typeof o.wallet !== "string") return null;
  const p = o.payload;
  return {
    id: o.id,
    kind: o.kind,
    wallet: o.wallet,
    market_id: typeof o.market_id === "string" ? o.market_id : null,
    payload: p && typeof p === "object" && !Array.isArray(p) ? (p as Record<string, unknown>) : {},
    created_at: typeof o.created_at === "string" ? o.created_at : null,
  };
}

function toFeedItem(head: HeadId, v: unknown): FeedItem | null {
  if (head === "solana") {
    const a = toAlertRow(v);
    return a ? { head, key: `solana:${a.id}`, id: a.id, at: a.block_time, alert: a } : null;
  }
  const a = toHlAlert(v);
  return a ? { head, key: `hyperliquid:${a.id}`, id: a.id, at: a.created_at, alert: a } : null;
}

/** A head-tagged SSE `alert` payload; null if untagged, unknown head or the wrong shape. */
export function parseFeedEvent(data: string): FeedItem | null {
  let v: unknown;
  try {
    v = JSON.parse(data);
  } catch {
    return null;
  }
  const head = v && typeof v === "object" ? (v as Record<string, unknown>).head : undefined;
  return typeof head === "string" && isHeadId(head) ? toFeedItem(head, v) : null;
}

/** A head's decoded alert list (GET /api/alerts of that head) as feed items. */
export function toFeedItems(head: HeadId, data: unknown): FeedItem[] {
  if (!Array.isArray(data)) return [];
  return data.map((v) => toFeedItem(head, v)).filter((i): i is FeedItem => i !== null);
}

function timeOf(i: FeedItem): number {
  const t = i.at ? Date.parse(i.at) : NaN;
  return Number.isNaN(t) ? -Infinity : t;
}

/** Dedupe by head+id (ids of different heads never collide), newest first, capped. */
export function mergeFeed(existing: FeedItem[], incoming: FeedItem[], cap: number = FEED_CAP): FeedItem[] {
  const byKey = new Map<string, FeedItem>();
  for (const i of existing) byKey.set(i.key, i);
  for (const i of incoming) byKey.set(i.key, i);
  return [...byKey.values()]
    .sort((a, b) => {
      const ta = timeOf(a);
      const tb = timeOf(b);
      if (ta !== tb) return tb > ta ? 1 : -1;
      return b.id - a.id || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
    })
    .slice(0, Math.max(0, cap));
}

export function filterFeed(items: FeedItem[], filter: FeedFilter): FeedItem[] {
  return filter === "all" ? items : items.filter((i) => i.head === filter);
}

/** SSE `head` event of the fan-in: one head's upstream stream went up or down. */
export function parseHeadStatus(data: string): { head: HeadId; up: boolean } | null {
  try {
    const v = JSON.parse(data) as Record<string, unknown>;
    return typeof v.head === "string" && isHeadId(v.head) && typeof v.up === "boolean" ? { head: v.head, up: v.up } : null;
  } catch {
    return null;
  }
}

export type HeadUp = Partial<Record<HeadId, boolean>>;

/**
 * Apply a fan-in `head` status event. `backfill` names the head whose stream came back
 * up after being down: what it emitted meanwhile is fetched (the first up is covered by
 * the server render; a browser-level reconnect resets the map and backfills every head).
 */
export function applyHeadStatus(prev: HeadUp, s: { head: HeadId; up: boolean }): { next: HeadUp; backfill: HeadId | null } {
  return { next: { ...prev, [s.head]: s.up }, backfill: s.up && prev[s.head] === false ? s.head : null };
}

export const BACKFILL_LIMIT = 50;

/** One head's recent alerts via the same-origin proxy (keys stay server-side); [] on any failure. */
export async function fetchHeadBackfill(head: HeadId, fetchImpl: typeof fetch = fetch, limit: number = BACKFILL_LIMIT): Promise<FeedItem[]> {
  try {
    const res = await fetchImpl(`/api/heads/${head}/alerts?limit=${limit}`, { cache: "no-store" });
    if (!res.ok) return [];
    return toFeedItems(head, await res.json());
  } catch {
    return [];
  }
}
