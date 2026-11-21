// Paper Arena (spec 10): followed wallets across heads ranked by OUR paper result, and
// the pure helpers of the paper detail page. Pure: safe for server and client components.

import type { PnlPoint } from "./api";
import { formatDuration, formatPct, formatUsd } from "./format";
import { HEADS, type HeadId } from "./heads";
import { num } from "./hlView";

/** Decimals arrive as JSON strings; null where a follow has no paper result yet. */
type Dec = string | number | null;

/** core/hades_core/contract.py PaperSummary (tolerant reader). */
export interface PaperSummary {
  address: string;
  ladder: string;
  equity_usd: Dec;
  net_pnl_usd: Dec;
  max_dd: Dec;
  closed_trades: number | null;
  days_followed: number | null;
  gap_usd: Dec;
  origin: string;
}

/** A PaperSummary plus the follow row's downsampled equity sparkline ([iso, decimal]). */
export interface ArenaEntry extends PaperSummary {
  sparkline?: unknown[];
}

/** One head's answer: its entries, or why there are none. */
export type HeadState = ArenaEntry[] | "offline" | "unavailable" | { status: number };

export type ArenaRow =
  | {
      kind: "wallet";
      head: HeadId;
      address: string;
      ladder: string;
      origin: string;
      netPnl: number | null;
      maxDd: number | null;
      days: number | null;
      closed: number | null;
      gap: number | null;
      sparkline: number[];
    }
  | { kind: "head"; head: HeadId; state: "offline" | "unavailable" }
  | { kind: "head"; head: HeadId; state: "error"; status: number };

/** Closed paper trades required by the gate (plan global); the follows list carries no config. */
export const ARENA_MIN_CLOSED = 30;

function sparkValues(v: unknown): number[] {
  if (!Array.isArray(v)) return [];
  const out: number[] = [];
  for (const p of v) {
    const n = Array.isArray(p) ? num(p[1]) : null;
    if (n != null) out.push(n);
  }
  return out;
}

const int = (v: unknown) => {
  const n = num(v);
  return n != null && Number.isInteger(n) ? n : null;
};

/**
 * Wallet rows of every head, by our net paper PnL (highest first; no result yet last,
 * ties by address), then one row per head that is offline / not available / erroring,
 * in registry order: a down head is listed separately, never ranked.
 */
export function arenaRows(perHead: Partial<Record<HeadId, HeadState>>): ArenaRow[] {
  const wallets: ArenaRow[] = [];
  const heads: ArenaRow[] = [];
  for (const { id } of HEADS) {
    const state = perHead[id];
    if (state === undefined) continue;
    if (state === "offline" || state === "unavailable") {
      heads.push({ kind: "head", head: id, state });
      continue;
    }
    if (!Array.isArray(state)) {
      heads.push({ kind: "head", head: id, state: "error", status: state.status });
      continue;
    }
    for (const e of state) {
      wallets.push({
        kind: "wallet",
        head: id,
        address: e.address,
        ladder: e.ladder,
        origin: e.origin,
        netPnl: num(e.net_pnl_usd),
        maxDd: num(e.max_dd),
        days: int(e.days_followed),
        closed: int(e.closed_trades),
        gap: num(e.gap_usd),
        sparkline: sparkValues(e.sparkline),
      });
    }
  }
  const key = (r: ArenaRow) => (r.kind === "wallet" && r.netPnl != null ? r.netPnl : -Infinity);
  wallets.sort((a, b) => {
    const d = key(b) - key(a);
    if (d !== 0 && !Number.isNaN(d)) return d;
    const aa = a.kind === "wallet" ? a.address : "";
    const bb = b.kind === "wallet" ? b.address : "";
    return aa < bb ? -1 : aa > bb ? 1 : 0;
  });
  return [...wallets, ...heads];
}

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

/** GET /api/follows (hyperliquid/hades_hl/api.py FollowRow[]) as arena entries. */
export function followsToEntries(data: unknown): ArenaEntry[] {
  if (!Array.isArray(data)) return [];
  const out: ArenaEntry[] = [];
  for (const v of data) {
    const f = obj(v);
    if (!f || typeof f.address !== "string") continue;
    const s = obj(f.summary);
    const origin = typeof f.origin === "string" ? f.origin : "manual";
    const sparkline = Array.isArray(f.sparkline) ? f.sparkline : [];
    if (s) {
      // The follow row's ladder is authoritative; the summary is the last snapshot's.
      const ladder = typeof f.ladder === "string" ? f.ladder : (s as unknown as PaperSummary).ladder;
      out.push({ ...(s as unknown as PaperSummary), ladder, address: f.address, origin, sparkline });
    } else {
      out.push({
        address: f.address,
        ladder: typeof f.ladder === "string" ? f.ladder : "PAPER",
        equity_usd: null,
        net_pnl_usd: null,
        max_dd: null,
        closed_trades: null,
        days_followed: null,
        gap_usd: null,
        origin,
        sparkline,
      });
    }
  }
  return out;
}

// -- gate checklist ---------------------------------------------------------------------

/** core/hades_core/gate.py GateResult. */
export interface GateResult {
  passed: boolean;
  checks: Record<string, boolean>;
  values: Record<string, string>;
}

/** hades_hl.paper_runtime.gate_config(): the thresholds the gate was evaluated with. */
export interface GateConfig {
  min_days?: number;
  min_closed?: number;
  max_dd?: string | number;
  max_top_share?: string | number;
  rolling_days?: number;
}

export interface GateStatus {
  full: GateResult;
  rolling: GateResult;
  config: GateConfig;
}

export interface GateRow {
  key: string;
  label: string;
  passed: boolean;
  value: string;
  threshold: string;
}

const DASH = "—";

function pctOr(v: unknown): string {
  const n = num(v);
  return n == null ? (typeof v === "string" && v ? v : DASH) : formatPct(n);
}

function usdOr(v: unknown): string {
  const n = num(v);
  return n == null ? (typeof v === "string" && v ? v : DASH) : formatUsd(n);
}

/**
 * One row per check the API returned (in a stable order: known checks first), with
 * its value from `values` and its threshold from the API's gate `config`. Net PnL's
 * "> $0" is structural (not configurable), so it is the one fixed threshold.
 */
export function gateRows(result: GateResult, config: GateConfig, scope: "full" | "rolling"): GateRow[] {
  const v = result.values ?? {};
  const window = v.window_days ?? (config.rolling_days != null ? String(config.rolling_days) : null);
  const ORDER = ["days", "closed_trades", "net_pnl", "max_dd", "top_share"];
  const keys = Object.keys(result.checks ?? {}).sort((a, b) => {
    const ia = ORDER.indexOf(a);
    const ib = ORDER.indexOf(b);
    return (ia < 0 ? ORDER.length : ia) - (ib < 0 ? ORDER.length : ib);
  });
  const gte = (n: unknown, unit = "") => (num(n) == null ? DASH : `≥ ${num(n)}${unit}`);
  return keys.map((key): GateRow => {
    const passed = result.checks[key] === true;
    switch (key) {
      case "days":
        return { key, label: "Days followed", passed, value: v.days != null ? `${v.days} d` : DASH, threshold: gte(config.min_days, " d") };
      case "closed_trades":
        return { key, label: "Closed paper trades", passed, value: v.closed_trades ?? DASH, threshold: gte(config.min_closed) };
      case "net_pnl": {
        const label = scope === "rolling" && window ? `Net paper PnL (last ${window} d)` : "Net paper PnL";
        let value = usdOr(v.net_pnl);
        if (scope === "full" && v.realized_net_pnl != null) value += ` (realized ${usdOr(v.realized_net_pnl)})`;
        return { key, label, passed, value, threshold: "> $0" };
      }
      case "max_dd":
        return {
          key,
          label: scope === "rolling" && window ? `Max drawdown (last ${window} d)` : "Max drawdown",
          passed,
          value: pctOr(v.max_dd),
          threshold: num(config.max_dd) == null ? DASH : `< ${formatPct(num(config.max_dd))}`,
        };
      case "top_share":
        return {
          key,
          label: "Top trade share",
          passed,
          value: v.top_share === "n/a" ? "n/a" : pctOr(v.top_share),
          threshold: num(config.max_top_share) == null ? DASH : `≤ ${formatPct(num(config.max_top_share))} of net profit`,
        };
      default:
        return { key, label: key, passed, value: v[key] ?? DASH, threshold: DASH };
    }
  });
}

// -- paper detail (GET /api/paper/{address}) ---------------------------------------------

export interface LadderStep {
  from: string | null;
  to: string;
  at: string;
  gate: Record<string, unknown>;
}

/** hyperliquid/hades_hl/api.py PaperDetail (read tolerantly by the detail page). */
export interface PaperDetail {
  address: string;
  active: boolean;
  follow_started_at: string;
  computed_at: string | null;
  summary: PaperSummary | null;
  winrate: number | null;
  n: number;
  /** The latest paper fills (paper_runtime `_fill_json`); read with readPaperFills. */
  fills?: unknown;
  /** Our open paper positions; read with readPaperPositions. */
  open_positions?: unknown;
  equity_curve: unknown;
  gate: GateStatus | null;
  ladder_history: LadderStep[];
  /** ISO time until which a promotion is held back after a demotion; null otherwise. */
  ladder_cooldown_until?: string | null;
  gap_usd: Dec;
  /** The leader's own realized PnL, raw (P49): shown separately from the gap. */
  leader_realized_usd: Dec;
  missed_count: number;
  median_delay_ms: number | null;
  funding_missing_hours: number | null;
}

/** `850 ms`, `2.4 s`, then the compact duration (`3m 5s`); a dash when unknown. */
export function formatDelayMs(ms: number | null | undefined): string {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return DASH;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return formatDuration(ms / 1000);
}

// -- paper fills and open positions -------------------------------------------------------

/** The head keeps the latest 200 fills in the snapshot (paper_runtime FILLS_KEEP). */
export const PAPER_FILLS_KEEP = 200;

export interface PaperFillRow {
  ref: string;
  /**
   * "fill": a copy of a leader fill (`hash:tid[:open|:close]`); "liquidation": our own
   * simulated liquidation; "reconcile": a synthetic close (the leader was flat, P11).
   */
  source: "fill" | "liquidation" | "reconcile";
  /** The leader's transaction hash, when the ref carries one. */
  hash: string | null;
  coin: string;
  side: string;
  direction: string;
  /** Our size (0 when missed). */
  size: number | null;
  /** Our modelled fill price; null when the event was missed. */
  price: number | null;
  fee: number | null;
  realizedPnl: number | null;
  missed: boolean;
  positionId: string | null;
  at: string | null;
}

const TX_HASH = /^0x[0-9a-fA-F]{64}$/;
const text = (v: unknown) => (typeof v === "string" ? v : null);

/** `fills` of GET /api/paper/{address}, newest first. Every row traces to its `ref`. */
export function readPaperFills(v: unknown): PaperFillRow[] {
  if (!Array.isArray(v)) return [];
  const out: PaperFillRow[] = [];
  for (const raw of v) {
    const f = obj(raw);
    if (!f || typeof f.ref !== "string" || typeof f.market_id !== "string") continue;
    const source = f.ref.startsWith("liquidation:") ? "liquidation" : f.ref.startsWith("reconcile:") ? "reconcile" : "fill";
    const head = f.ref.split(":")[0];
    out.push({
      ref: f.ref,
      source,
      hash: source === "fill" && TX_HASH.test(head) ? head : null,
      coin: f.market_id,
      side: text(f.side) ?? "",
      direction: text(f.direction) ?? "",
      size: num(f.size),
      price: num(f.price),
      fee: num(f.fee),
      realizedPnl: num(f.realized_pnl),
      missed: f.missed === true,
      positionId: text(f.position_id),
      at: text(f.at),
    });
  }
  return out.reverse();
}

export interface PaperPositionRow {
  positionId: string;
  coin: string;
  direction: string;
  qty: number | null;
  entryPrice: number | null;
  margin: number | null;
  leverage: number | null;
  /** The leader's size in this position (their units), for scale. */
  leaderSize: number | null;
}

/** `open_positions` of GET /api/paper/{address}. */
export function readPaperPositions(v: unknown): PaperPositionRow[] {
  if (!Array.isArray(v)) return [];
  const out: PaperPositionRow[] = [];
  for (const raw of v) {
    const p = obj(raw);
    if (!p || typeof p.market_id !== "string") continue;
    out.push({
      positionId: text(p.position_id) ?? p.market_id,
      coin: p.market_id,
      direction: text(p.direction) ?? "",
      qty: num(p.qty),
      entryPrice: num(p.entry_price),
      margin: num(p.margin),
      leverage: num(p.leverage),
      leaderSize: num(p.leader_size),
    });
  }
  return out;
}

// -- equity curve -----------------------------------------------------------------------

/** `[[iso, decimal], …]` equity points as chart points (USD carried in cum_pnl_sol). */
export function equityPoints(v: unknown): PnlPoint[] {
  if (!Array.isArray(v)) return [];
  const out: PnlPoint[] = [];
  for (const p of v) {
    if (!Array.isArray(p) || typeof p[0] !== "string") continue;
    const n = num(p[1]);
    if (n != null) out.push({ t: p[0], cum_pnl_sol: n });
  }
  return out;
}

/** Fall from the running peak at each point, as a non-positive fraction (0 = at the peak). */
export function drawdownCurve(points: PnlPoint[]): PnlPoint[] {
  let peak = -Infinity;
  return points.map((p) => {
    peak = Math.max(peak, p.cum_pnl_sol);
    const dd = peak > 0 ? (p.cum_pnl_sol - peak) / peak : 0;
    return { t: p.t, cum_pnl_sol: dd === 0 ? 0 : dd };
  });
}

/** SVG path of a sparkline in a w×h box (highest value at the top); "" below 2 points. */
export function sparklinePath(values: number[], w: number, h: number): string {
  if (values.length < 2) return "";
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const step = w / (values.length - 1);
  return values
    .map((v, i) => {
      const y = hi === lo ? h / 2 : h - ((v - lo) / (hi - lo)) * h;
      return `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}
