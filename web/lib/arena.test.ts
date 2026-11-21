import { describe, expect, it } from "vitest";
import {
  arenaRows,
  drawdownCurve,
  followsToEntries,
  formatDelayMs,
  gateRows,
  sparklinePath,
  type ArenaEntry,
  type GateConfig,
  type GateResult,
} from "./arena";

const A = "0x" + "aa".repeat(20);
const B = "0x" + "bb".repeat(20);
const C = "0x" + "cc".repeat(20);

function entry(address: string, net: string | null, extra: Partial<ArenaEntry> = {}): ArenaEntry {
  return {
    address,
    ladder: "PAPER",
    equity_usd: "1000",
    net_pnl_usd: net,
    max_dd: "0.05",
    closed_trades: 12,
    days_followed: 9,
    gap_usd: "-3.5",
    origin: "auto",
    ...extra,
  };
}

describe("arenaRows", () => {
  it("ranks followed wallets across heads by our net paper PnL, highest first", () => {
    const rows = arenaRows({
      hyperliquid: [entry(A, "12.5"), entry(B, "-40"), entry(C, "310.25")],
    });
    const wallets = rows.filter((r) => r.kind === "wallet");
    expect(wallets.map((r) => (r.kind === "wallet" ? r.address : ""))).toEqual([C, A, B]);
    expect(wallets[0]).toMatchObject({ head: "hyperliquid", netPnl: 310.25, maxDd: 0.05, closed: 12, days: 9, gap: -3.5 });
  });

  it("puts wallets without a paper result yet after every ranked wallet", () => {
    const rows = arenaRows({ hyperliquid: [entry(A, null), entry(B, "-1")] });
    expect(rows.map((r) => (r.kind === "wallet" ? r.address : r.head))).toEqual([B, A]);
  });

  it("lists an offline head separately, after the ranked wallets, never as a wallet", () => {
    const rows = arenaRows({ solana: "offline", hyperliquid: [entry(A, "1"), entry(B, "2")] });
    expect(rows).toHaveLength(3);
    expect(rows[0]).toMatchObject({ kind: "wallet", address: B });
    expect(rows[1]).toMatchObject({ kind: "wallet", address: A });
    expect(rows[2]).toEqual({ kind: "head", head: "solana", state: "offline" });
  });

  it("separates a head without the paper route (not available) from an HTTP error", () => {
    const rows = arenaRows({ solana: "unavailable", hyperliquid: { status: 500 } });
    expect(rows).toEqual([
      { kind: "head", head: "solana", state: "unavailable" },
      { kind: "head", head: "hyperliquid", state: "error", status: 500 },
    ]);
  });

  it("carries the sparkline values", () => {
    const rows = arenaRows({
      hyperliquid: [entry(A, "1", { sparkline: [["2026-11-01T00:00:00Z", "1000"], ["2026-11-02T00:00:00Z", "1001.5"]] })],
    });
    expect(rows[0]).toMatchObject({ sparkline: [1000, 1001.5] });
  });
});

describe("followsToEntries", () => {
  it("reads /api/follows rows, keeping a follow that has no paper summary yet", () => {
    const out = followsToEntries([
      { address: A, origin: "manual", started_at: "2026-11-01T00:00:00Z", ladder: "NOTIF", summary: entry(A, "5"), sparkline: [["t", "1"]] },
      { address: B, origin: "auto", started_at: "2026-11-01T00:00:00Z", ladder: "PAPER", summary: null, sparkline: [] },
      { nope: true },
    ]);
    expect(out).toHaveLength(2);
    expect(out[0]).toMatchObject({ address: A, net_pnl_usd: "5", sparkline: [["t", "1"]] });
    expect(out[1]).toMatchObject({ address: B, ladder: "PAPER", origin: "auto", net_pnl_usd: null });
  });

  it("returns [] for a non-array body", () => {
    expect(followsToEntries({ detail: "x" })).toEqual([]);
  });
});

// Deliberately NOT the plan defaults: thresholds must come from the API's gate config.
const CONFIG: GateConfig = { min_days: 14, min_closed: 7, max_dd: "0.15", max_top_share: "0.4", rolling_days: 21 };

describe("gateRows", () => {
  const full: GateResult = {
    passed: false,
    checks: { days: true, closed_trades: false, net_pnl: true, max_dd: true, top_share: false },
    values: {
      days: "16",
      closed_trades: "5",
      net_pnl: "42.5",
      realized_net_pnl: "30",
      max_dd: "0.0712",
      top_share: "0.62",
    },
  };

  it("lists every check with its value, its threshold from the API config and pass/fail", () => {
    const rows = gateRows(full, CONFIG, "full");
    expect(rows.map((r) => r.key)).toEqual(["days", "closed_trades", "net_pnl", "max_dd", "top_share"]);
    expect(rows.find((r) => r.key === "days")).toMatchObject({ passed: true, value: "16 d", threshold: "≥ 14 d" });
    expect(rows.find((r) => r.key === "closed_trades")).toMatchObject({ passed: false, value: "5", threshold: "≥ 7" });
    expect(rows.find((r) => r.key === "net_pnl")).toMatchObject({ passed: true, value: "+$42.50 (realized +$30.00)", threshold: "> $0" });
    expect(rows.find((r) => r.key === "max_dd")).toMatchObject({ passed: true, value: "7.1%", threshold: "< 15.0%" });
    expect(rows.find((r) => r.key === "top_share")).toMatchObject({ passed: false, value: "62.0%", threshold: "≤ 40.0% of net profit" });
  });

  it("shows a non-evaluable top share as n/a", () => {
    const rows = gateRows({ ...full, values: { ...full.values, top_share: "n/a" } }, CONFIG, "full");
    expect(rows.find((r) => r.key === "top_share")?.value).toBe("n/a");
  });

  it("labels the rolling gate with its window from the API values", () => {
    const rolling: GateResult = {
      passed: true,
      checks: { net_pnl: true, max_dd: true },
      values: { window_days: "21", net_pnl: "3", max_dd: "0.01", closed_trades: "4" },
    };
    const rows = gateRows(rolling, CONFIG, "rolling");
    expect(rows.map((r) => r.key)).toEqual(["net_pnl", "max_dd"]);
    expect(rows[0]).toMatchObject({ label: "Net paper PnL (last 21 d)", value: "+$3.00", threshold: "> $0" });
    expect(rows[1]).toMatchObject({ value: "1.0%", threshold: "< 15.0%" });
  });

  it("still renders a check it does not know, with its raw value", () => {
    const rows = gateRows({ passed: false, checks: { mystery: false }, values: { mystery: "7" } }, CONFIG, "full");
    expect(rows).toEqual([{ key: "mystery", label: "mystery", passed: false, value: "7", threshold: "—" }]);
  });
});

describe("drawdownCurve", () => {
  it("is the fall from the running peak, as a non-positive fraction", () => {
    const dd = drawdownCurve([
      { t: "2026-11-01T00:00:00Z", cum_pnl_sol: 100 },
      { t: "2026-11-01T01:00:00Z", cum_pnl_sol: 120 },
      { t: "2026-11-01T02:00:00Z", cum_pnl_sol: 90 },
      { t: "2026-11-01T03:00:00Z", cum_pnl_sol: 130 },
    ]);
    expect(dd.map((p) => p.cum_pnl_sol)).toEqual([0, 0, -0.25, 0]);
    expect(dd.map((p) => p.t)).toEqual([
      "2026-11-01T00:00:00Z",
      "2026-11-01T01:00:00Z",
      "2026-11-01T02:00:00Z",
      "2026-11-01T03:00:00Z",
    ]);
  });

  it("is 0 while the peak is not positive", () => {
    expect(drawdownCurve([{ t: "a", cum_pnl_sol: 0 }, { t: "b", cum_pnl_sol: -5 }]).map((p) => p.cum_pnl_sol)).toEqual([0, 0]);
  });
});

describe("sparklinePath", () => {
  it("scales values into the box, highest at the top", () => {
    expect(sparklinePath([1, 3, 2], 100, 20)).toBe("M0.0,20.0 L50.0,0.0 L100.0,10.0");
  });

  it("draws a flat line mid-box when all values are equal, nothing for < 2 points", () => {
    expect(sparklinePath([5, 5], 10, 20)).toBe("M0.0,10.0 L10.0,10.0");
    expect(sparklinePath([5], 10, 20)).toBe("");
  });
});

describe("formatDelayMs", () => {
  it("formats a median delay for humans", () => {
    expect(formatDelayMs(850)).toBe("850 ms");
    expect(formatDelayMs(2400)).toBe("2.4 s");
    expect(formatDelayMs(185_000)).toBe("3m 5s");
    expect(formatDelayMs(null)).toBe("—");
  });
});
