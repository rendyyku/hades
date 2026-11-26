// /signals filters: URL search params <-> engine query <-> client-side match for live events.

import type { SignalRow } from "./api";

export const SOURCES = ["repo_diff", "ct"] as const;

export interface SignalQuery {
  source: string | null;
  chain: string | null;
  minScore: number | null;
  buyable: boolean | null;
}

export type SearchParams = Record<string, string | string[] | undefined>;

function first(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

export function parseSignalQuery(sp: SearchParams): SignalQuery {
  const source = first(sp.source);
  const chain = first(sp.chain);
  const min = first(sp.min_score);
  const buyable = first(sp.buyable);
  const minScore = min !== undefined && /^\d{1,3}$/.test(min) && Number(min) <= 100 ? Number(min) : null;
  return {
    source: source && (SOURCES as readonly string[]).includes(source) ? source : null,
    chain: chain && /^[a-z0-9_-]{1,32}$/.test(chain) ? chain : null,
    minScore,
    buyable: buyable === "true" ? true : buyable === "false" ? false : null,
  };
}

function params(q: SignalQuery): URLSearchParams {
  const p = new URLSearchParams();
  if (q.source) p.set("source", q.source);
  if (q.chain) p.set("chain", q.chain);
  if (q.minScore !== null) p.set("min_score", String(q.minScore));
  if (q.buyable !== null) p.set("buyable", String(q.buyable));
  return p;
}

export function signalsApiPath(q: SignalQuery, limit = 50): string {
  const p = params(q);
  p.set("limit", String(limit));
  return `/api/signals?${p.toString()}`;
}

export function matchesQuery(r: SignalRow, q: SignalQuery): boolean {
  if (q.source && r.source !== q.source) return false;
  if (q.chain && r.chain !== q.chain) return false;
  if (q.minScore !== null && (r.score ?? -1) < q.minScore) return false;
  if (q.buyable !== null && (r.enrichment?.buyable ?? null) !== q.buyable) return false;
  return true;
}
