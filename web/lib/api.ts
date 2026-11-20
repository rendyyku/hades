import "server-only";

// Server-side client for the HADES engine API. Never throws; never exposes the key.

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string; status?: number };

const DEFAULT_API_URL = "http://127.0.0.1:8000";
const TIMEOUT_MS = 8000;

export async function apiGet<T>(path: string): Promise<ApiResult<T>> {
  const base = (process.env.HADES_API_URL || DEFAULT_API_URL).replace(/\/+$/, "");
  let res: Response;
  try {
    res = await fetch(`${base}${path}`, {
      headers: { "X-Hades-Key": process.env.HADES_API_KEY ?? "", Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    // Network error, DNS failure or timeout. The original error is dropped on purpose.
    return { ok: false, error: "engine unreachable" };
  }
  if (!res.ok) {
    return { ok: false, error: `HTTP ${res.status}`, status: res.status };
  }
  try {
    return { ok: true, data: (await res.json()) as T };
  } catch {
    return { ok: false, error: "invalid response", status: res.status };
  }
}

// -- API shapes (mirror engine/hades/api/schemas.py; datetimes are ISO 8601 strings) --

export interface WalletRow {
  address: string;
  score: number | null;
  total_pnl_sol: number | null;
  realized_pnl_sol: number | null;
  win_rate: number | null;
  tokens_closed: number | null;
  tags: string[];
  is_bot: boolean;
}

export interface Overview {
  tracked_wallets: number;
  runner_tokens: number;
  alerts_24h: number;
  last_swap_at: string | null;
  top: WalletRow[];
}

export interface WalletList {
  items: WalletRow[];
  total: number;
}

export interface WalletStatsOut {
  address: string;
  window_days: number | null;
  realized_pnl_sol: number | null;
  unrealized_pnl_sol: number | null;
  total_pnl_sol: number | null;
  tokens_traded: number | null;
  tokens_closed: number | null;
  win_rate: number | null;
  median_hold_sec: number | null;
  trades_per_day: number | null;
  /** Cost basis of open positions (in the window) with no trusted price at rescore. */
  unpriced_cost_sol: number | null;
  score: number | null;
  computed_at: string | null;
}

export interface PositionRow {
  mint: string;
  symbol: string | null;
  bought_tokens: number;
  sold_tokens: number;
  cost_sol: number;
  proceeds_sol: number;
  realized_pnl_sol: number;
  remaining_tokens: number;
  untracked_sell_tokens: number;
  /** Open cost basis (the read API never prices positions); 0 when closed. */
  unpriced_cost_sol: number;
  closed: boolean;
  first_buy_time: string | null;
  last_sell_time: string | null;
}

export interface TagRow {
  tag: string;
  mint: string | null;
  evidence: Record<string, unknown>;
  created_at: string | null;
}

export interface FundingHop {
  address: string;
  funding_signature: string | null;
  funding_slot: number | null;
}

export interface PnlPoint {
  t: string;
  cum_pnl_sol: number;
}

export interface WalletDetail {
  /** Stats window in days; positions and the PnL curve are all-time. */
  window_days: number;
  is_bot: boolean;
  min_tokens_closed: number;
  stats: WalletStatsOut | null;
  positions: PositionRow[];
  tags: TagRow[];
  funding_chain: FundingHop[];
  pnl_curve: PnlPoint[];
}

export interface TokenOut {
  mint: string;
  symbol: string | null;
  name: string | null;
  deployer: string | null;
  pool_address: string | null;
  pool_created_slot: number | null;
  pool_created_at: string | null;
  first_seen_at: string | null;
  peak_mcap_usd: number | null;
  is_runner: boolean | null;
  /** Wallet.first_funder of the deployer. */
  deployer_funder: string | null;
  /** Signature scan hit its cap: deployer / launch slot unknown, buyers are earliest observed. */
  history_truncated: boolean;
}

export interface EarlyBuyer {
  address: string;
  slot: number;
  signature: string;
  sol_amount: number | null;
  tags: string[];
  /** Wallet.first_funder of the buyer. */
  funder: string | null;
  /** True when the funder is labeled 'hub'. */
  funder_is_hub: boolean;
}

export interface Bundle {
  cluster: string;
  funder: string | null;
  wallets: string[];
}

export interface TokenForensics {
  token: TokenOut;
  early_buyers: EarlyBuyer[];
  bundles: Bundle[];
}

export interface WatchlistRow {
  address: string;
  note: string | null;
  added_at: string | null;
}

/** Payload of the SSE `alert` event. */
export interface AlertRow {
  id: number;
  wallet: string;
  mint: string;
  symbol: string | null;
  side: string;
  sol_amount: number | null;
  signature: string;
  block_time: string | null;
}
