// Cerberus live-feed state helpers. Pure: safe for server and client components.

import type { AlertRow } from "./api";
import { relTime } from "./format";

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
