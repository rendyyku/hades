// Pure display formatters. Safe for server and client components.

const DASH = "—";
const MINUS = "\u2212";

const twoDp = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const fourSig = new Intl.NumberFormat("en-US", {
  minimumSignificantDigits: 4,
  maximumSignificantDigits: 4,
  useGrouping: false,
});

function isNum(n: number | null | undefined): n is number {
  return typeof n === "number" && Number.isFinite(n);
}

/** `+12.34 SOL` / `−3.10 SOL` (U+2212). 2 decimals for |n| ≥ 0.01, else 4 significant digits. */
export function formatSol(n: number | null | undefined): string {
  if (!isNum(n)) return DASH;
  if (n === 0) return "0.00 SOL";
  const abs = Math.abs(n);
  const body = abs >= 0.01 ? twoDp.format(abs) : fourSig.format(abs);
  return `${n > 0 ? "+" : MINUS}${body} SOL`;
}

/** Ratio → percent with one decimal: `0.634` → `63.4%`. */
export function formatPct(x: number | null | undefined): string {
  if (!isNum(x)) return DASH;
  return `${(x * 100).toFixed(1)}%`;
}

/** First 4 + `…` + last 4. */
export function shortAddr(a: string | null | undefined): string {
  if (!a) return DASH;
  if (a.length <= 9) return a;
  return `${a.slice(0, 4)}…${a.slice(-4)}`;
}

/** Score with one decimal. */
export function formatScore(s: number | null | undefined): string {
  if (!isNum(s)) return DASH;
  return s.toFixed(1);
}

/** Compact relative time: `12s ago`, `5m ago`, `3h ago`, `2d ago`. */
export function relTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return DASH;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return DASH;
  const sec = Math.max(0, Math.floor((now.getTime() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.floor(hr / 24)}d ago`;
}

/** Compact duration from seconds: `45s`, `2m 5s`, `1h 0m`, `1d 1h`. */
export function formatDuration(sec: number | null | undefined): string {
  if (!isNum(sec) || sec < 0) return DASH;
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

const usd = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** `+$1,234.50` / `−$3.10` (U+2212); `$0.00`. `signed: false` drops the + (negatives keep −). */
export function formatUsd(n: number | null | undefined, opts: { signed?: boolean } = {}): string {
  if (!isNum(n)) return DASH;
  const body = `$${usd.format(Math.abs(n))}`;
  if (n < 0) return `${MINUS}${body}`;
  return n === 0 || opts.signed === false ? body : `+${body}`;
}

/**
 * Winrate with its sample size, `64% (n=42)`. Never a bare percentage: without a
 * valid n (a positive integer) there is no percent at all.
 */
export function formatWinrate(w: number | null | undefined, n: number | null | undefined): string {
  const validN = isNum(n) && Number.isInteger(n) && n >= 0;
  if (!validN) return DASH;
  if (!isNum(w) || n === 0) return `${DASH} (n=${n})`;
  return `${Math.round(w * 100)}% (n=${n})`;
}
