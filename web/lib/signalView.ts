// Display helpers for signals. Pure: safe for server and client components.

import type { EnrichmentRow, SignalRow } from "./api";

const DASH = "—";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WIB_OFFSET_MS = 7 * 3600_000;

export type Tone = "ember" | "gain" | "primary" | "muted";

export type BuyableLabel = "Belum bisa dibeli" | "Bisa dibeli" | "Kandidat" | "Tidak diketahui";

export function buyableState(e: EnrichmentRow | null | undefined): { label: BuyableLabel; tone: Tone } {
  if (!e) return { label: "Tidak diketahui", tone: "muted" };
  if (!e.verified && e.candidates.length) return { label: "Kandidat", tone: "muted" };
  if (e.buyable === true) return { label: "Bisa dibeli", tone: "gain" };
  if (e.buyable === false) return { label: "Belum bisa dibeli", tone: "ember" };
  return { label: "Tidak diketahui", tone: "muted" };
}

function trim(n: number, digits: number): string {
  return n.toFixed(digits).replace(/\.?0+$/, "");
}

/** `$800`, `$12.5k`, `$1.2M`, `—`. */
export function formatLiquidity(usd: number | null | undefined): string {
  if (typeof usd !== "number" || !Number.isFinite(usd)) return DASH;
  if (usd >= 1_000_000) return `$${trim(usd / 1_000_000, 1)}M`;
  if (usd >= 1_000) return `$${trim(usd / 1_000, 1)}k`;
  return `$${Math.round(usd)}`;
}

export function scoreTone(score: number | null | undefined): Tone {
  if (typeof score !== "number") return "muted";
  if (score >= 70) return "ember";
  if (score >= 40) return "primary";
  return "muted";
}

/** `27 Nov 2026 14:05 WIB` (UTC+7, no DST), independent of the server/browser time zone. */
export function wib(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return DASH;
  const d = new Date(t + WIB_OFFSET_MS);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()} ${hh}:${mm} WIB`;
}

/** CT signals are named by their subdomain; token signals by `SYMBOL — Name`. */
export function signalLabel(s: Pick<SignalRow, "source" | "symbol" | "name">): string {
  if (s.source === "ct") return s.name || DASH;
  const parts = [s.symbol, s.name].filter((x): x is string => !!x);
  return parts.length ? parts.join(" — ") : DASH;
}

/** Only http(s) URLs become links; anything else (javascript:, data:) is shown as text. */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}

/** `github.com/a/b/pull/1` from `https://github.com/a/b/pull/1`. */
export function shortUrl(url: string): string {
  return url.replace(/^https?:\/\//, "").replace(/\/$/, "");
}

function leadAmount(hours: number): string {
  return hours >= 72 ? `${Math.floor(hours / 24)} hari` : `${Math.round(hours)} jam`;
}

/** `50 jam setelah sinyal`; from 72 h on, whole days: `3 hari setelah sinyal`. */
export function leadText(hours: number): string {
  return `${leadAmount(hours)} setelah sinyal`;
}

/** Same lead, worded from the listing's side: `50 jam sebelum listing`. */
export function leadBeforeListing(hours: number): string {
  return `${leadAmount(hours)} sebelum listing`;
}
