import { describe, expect, it } from "vitest";
import type { EnrichmentRow, SignalRow } from "./api";
import { buyableState, formatLiquidity, scoreTone, signalLabel, wib } from "./signalView";

const E = (p: Partial<EnrichmentRow>): EnrichmentRow => ({
  id: 1,
  signal_id: 1,
  checked_at: "2026-11-27T07:05:00+00:00",
  provider: "geckoterminal",
  buyable: null,
  liquidity_usd: null,
  dex: null,
  pool_url: null,
  volume_24h: null,
  pool_count: 0,
  verified: true,
  candidates: [],
  error: null,
  ...p,
});

describe("buyableState", () => {
  it("covers the four cases", () => {
    expect(buyableState(E({ buyable: true, liquidity_usd: 5000 }))).toEqual({ label: "Bisa dibeli", tone: "gain" });
    expect(buyableState(E({ buyable: false }))).toEqual({ label: "Belum bisa dibeli", tone: "ember" });
    expect(buyableState(E({ buyable: true, verified: false, candidates: [{ chain: "monad" }] }))).toEqual({
      label: "Kandidat",
      tone: "muted",
    });
    expect(buyableState(E({ buyable: null, error: "x" }))).toEqual({ label: "Tidak diketahui", tone: "muted" });
    expect(buyableState(null)).toEqual({ label: "Tidak diketahui", tone: "muted" });
  });
});

describe("formatLiquidity", () => {
  it("formats compact USD", () => {
    expect(formatLiquidity(12500)).toBe("$12.5k");
    expect(formatLiquidity(1_200_000)).toBe("$1.2M");
    expect(formatLiquidity(800)).toBe("$800");
    expect(formatLiquidity(3000)).toBe("$3k");
    expect(formatLiquidity(null)).toBe("—");
  });
});

describe("scoreTone", () => {
  it("ember from 70, primary from 40, else muted", () => {
    expect([scoreTone(70), scoreTone(69), scoreTone(40), scoreTone(39), scoreTone(null)]).toEqual([
      "ember",
      "primary",
      "primary",
      "muted",
      "muted",
    ]);
  });
});

describe("wib", () => {
  it("shows WIB time", () => {
    expect(wib("2026-11-27T07:05:00+00:00")).toBe("27 Nov 2026 14:05 WIB");
    expect(wib(null)).toBe("—");
  });
});

describe("signalLabel", () => {
  const base = { source: "repo_diff", symbol: "CAT", name: "Cash Cat" } as SignalRow;
  it("uses symbol — name for tokens and the subdomain for CT", () => {
    expect(signalLabel(base)).toBe("CAT — Cash Cat");
    expect(signalLabel({ ...base, source: "ct", symbol: null, name: "claim.cat.xyz" })).toBe("claim.cat.xyz");
    expect(signalLabel({ ...base, symbol: null, name: null })).toBe("—");
  });
});
