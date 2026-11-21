// Typed views of the Hyperliquid head responses (spec 9 contract). Pure.

import type { PnlPoint } from "./api";

/** core/hades_core/contract.py WalletRow. Decimals arrive as JSON strings. */
export interface HlWalletRow {
  address: string;
  score: number | null;
  net_pnl_usd: string | number;
  trades: number;
  /** Null when n is 0: a winrate is never shown without n. */
  winrate: number | null;
  n: number;
  tags: string[];
  is_bot: boolean;
  extra: Record<string, unknown>;
}

export interface HlFollowInfo {
  origin: string;
  started_at: string;
  ladder: string;
  active: boolean;
}

export interface HlEvidenceRow {
  tid: number | null;
  hash: string | null;
  coin: string | null;
  /** ISO time of the fill. */
  time: string | null;
  direction: string | null;
  notionalUsd: number | null;
}

export interface HlTag {
  tag: string;
  created_at: string | null;
  evidence: HlEvidenceRow[];
}

const HASH = /^0x[0-9a-fA-F]{64}$/;
const ADDRESS = /^0x[0-9a-fA-F]{40}$/;

export function hlTxUrl(hash: string | null | undefined): string | null {
  return typeof hash === "string" && HASH.test(hash) ? `https://app.hyperliquid.xyz/explorer/tx/${hash}` : null;
}

export function hlExplorerAddress(address: string): string | null {
  return ADDRESS.test(address) ? `https://app.hyperliquid.xyz/explorer/address/${address}` : null;
}

/** Number from a JSON number or decimal string; null otherwise. */
export function num(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

const str = (v: unknown) => (typeof v === "string" ? v : null);

function readRow(v: unknown): HlEvidenceRow | null {
  const r = obj(v);
  if (!r) return null;
  const detail = obj(r.detail) ?? {};
  const ms = num(r.time);
  return {
    tid: num(r.tid),
    hash: str(r.hash),
    coin: str(r.coin),
    time: ms != null ? new Date(ms).toISOString() : null,
    direction: str(detail.direction),
    notionalUsd: num(detail.notional_usd),
  };
}

/** `extra.tags` of GET /api/wallets/{address}: tags with their evidence rows. */
export function readHlTags(v: unknown): HlTag[] {
  if (!Array.isArray(v)) return [];
  const out: HlTag[] = [];
  for (const t of v) {
    const o = obj(t);
    if (!o || typeof o.tag !== "string") continue;
    const ev = Array.isArray(o.evidence) ? o.evidence.map(readRow).filter((x): x is HlEvidenceRow => x !== null) : [];
    out.push({ tag: o.tag, created_at: str(o.created_at), evidence: ev });
  }
  return out;
}

/** `extra.pnl_curve` ([iso, decimal-string] pairs) as PnlChart points (USD values). */
export function hlCurve(v: unknown): PnlPoint[] {
  if (!Array.isArray(v)) return [];
  const out: PnlPoint[] = [];
  for (const p of v) {
    if (!Array.isArray(p) || typeof p[0] !== "string") continue;
    const n = num(p[1]);
    if (n != null) out.push({ t: p[0], cum_pnl_sol: n });
  }
  return out;
}

export function readFollow(v: unknown): HlFollowInfo | null {
  const o = obj(v);
  if (!o || typeof o.ladder !== "string") return null;
  return {
    origin: str(o.origin) ?? "manual",
    started_at: str(o.started_at) ?? "",
    ladder: o.ladder,
    active: o.active === true,
  };
}

export const HL_PNL_EMPTY_TEXT = "No fills in the stats window";

export function hlPnlCaption(n: number, finalText: string): string {
  return `Cumulative net PnL (closed PnL minus fees plus funding) over ${n} active ${n === 1 ? "day" : "days"}, ending at ${finalText}`;
}
