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

// -- API shapes (mirror engine/hades/api.py + store.py rows; datetimes are ISO 8601 strings) --

export interface ScoreDetail {
  base: number;
  priority_chain: number;
  earliness: number;
  fusion: number;
  total: number;
}

export interface Candidate {
  chain?: string;
  name?: string;
  liquidity_usd?: number;
  pool_url?: string;
  dex?: string | null;
}

export interface EnrichmentRow {
  id: number;
  signal_id: number;
  checked_at: string;
  provider: string;
  buyable: boolean | null;
  liquidity_usd: number | null;
  dex: string | null;
  pool_url: string | null;
  volume_24h: number | null;
  pool_count: number;
  verified: boolean;
  candidates: Candidate[];
  error: string | null;
}

export interface SignalRow {
  id: number;
  dedupe_key: string;
  source: string;
  kind: string;
  severity: "high" | "low";
  project_key: string;
  chain: string | null;
  address: string | null;
  symbol: string | null;
  name: string | null;
  website: string | null;
  evidence_url: string;
  observed_at: string;
  score: number | null;
  score_detail: ScoreDetail | null;
  listed_on: { exchange: string; at: string }[];
  /** Latest enrichment; present on list rows. */
  enrichment?: EnrichmentRow | null;
  /** Present on the detail endpoint only. */
  raw?: Record<string, unknown>;
}

export interface SignalDetail {
  signal: SignalRow;
  enrichments: EnrichmentRow[];
  fusion: SignalRow[];
}

export interface WatcherRow {
  name: string;
  last_ok: string | null;
  last_err: string | null;
  last_err_at: string | null;
  fail_count: number;
  healthy: boolean;
}

export interface Overview {
  counts: { signals: number; alerts: number; listings: number };
  watchers: WatcherRow[];
  top: SignalRow[];
}

export interface ListingRow {
  id: number;
  exchange: string;
  symbol: string;
  base_asset: string;
  first_seen_at: string;
  earliest_signal: { id: number; source: string; kind: string; observed_at: string; lead_h: number } | null;
}

export interface DomainRow {
  domain: string;
  origin: "manual" | "auto";
  added_at: string;
}

export interface MuteRow {
  target: string;
  until: string;
}

export interface EvalRow {
  source: string;
  kind: string;
  signals: number;
  hits: number;
  false_positives: number;
  pending: number;
  median_lead_h: number | null;
  min_lead_h: number | null;
  small_sample: boolean;
}

export interface EvalReport {
  generated_at: string;
  window_days: number;
  by_source: EvalRow[];
  hits: { signal_id: number; listing_id: number; exchange: string; symbol: string; lead_h: number; weak: boolean }[];
  misses: { id: number; exchange: string; symbol: string; base_asset: string; first_seen_at: string }[];
}
