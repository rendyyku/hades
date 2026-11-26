// Live signal feed state helpers. Pure: safe for server and client components.

import type { SignalRow } from "./api";
import { relTime } from "./format";

/** LIVE only while the SSE is connected and the last event (signal or ping) is < 60 s old. */
export const LIVE_WINDOW_MS = 60_000;
export const FEED_CAP = 200;

export type LiveBadgeState = { label: "LIVE" } | { label: "STALE"; since: string };

export function liveBadge(connected: boolean, lastEventAt: number | null, now: number): LiveBadgeState {
  if (connected && lastEventAt != null && now - lastEventAt < LIVE_WINDOW_MS) return { label: "LIVE" };
  if (lastEventAt == null) return { label: "STALE", since: "never" };
  return { label: "STALE", since: relTime(new Date(lastEventAt).toISOString(), new Date(now)) };
}

/** Parse an SSE `signal` / `signal_updated` payload; null if it is not signal-shaped JSON. */
export function parseSignalEvent(data: string): SignalRow | null {
  let v: unknown;
  try {
    v = JSON.parse(data);
  } catch {
    return null;
  }
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const o = v as Record<string, unknown>;
  if (typeof o.id !== "number" || typeof o.source !== "string") return null;
  return o as unknown as SignalRow;
}

/**
 * Newest first. A row already in the list is replaced in place (an update), keeping its
 * enrichment when the event carries none; a new row is prepended. Capped at FEED_CAP.
 */
export function mergeSignal(list: SignalRow[], row: SignalRow, cap = FEED_CAP): SignalRow[] {
  const i = list.findIndex((r) => r.id === row.id);
  if (i >= 0) {
    const next = list.slice();
    next[i] = { ...list[i], ...row, enrichment: row.enrichment ?? list[i].enrichment };
    return next;
  }
  return [row, ...list].slice(0, cap);
}
