import { describe, expect, it } from "vitest";
import { hlCurve, hlExplorerAddress, hlTxUrl, num, readHlTags } from "./hlView";

const HASH = "0x" + "a1".repeat(32);

describe("hlTxUrl", () => {
  it("links a fill hash to the Hyperliquid explorer", () => {
    expect(hlTxUrl(HASH)).toBe(`https://app.hyperliquid.xyz/explorer/tx/${HASH}`);
  });
  it("refuses anything that is not a 32-byte hex hash", () => {
    expect(hlTxUrl("0x" + "0".repeat(10))).toBeNull();
    expect(hlTxUrl("javascript:alert(1)")).toBeNull();
    expect(hlTxUrl(null)).toBeNull();
  });
  it("explorer address link", () => {
    const a = "0x" + "ab".repeat(20);
    expect(hlExplorerAddress(a)).toBe(`https://app.hyperliquid.xyz/explorer/address/${a}`);
  });
});

describe("readHlTags", () => {
  it("reads tags with evidence rows", () => {
    const tags = readHlTags([
      {
        tag: "pre_move",
        created_at: "2026-11-20T00:00:00+00:00",
        evidence: [{ tid: 7, hash: HASH, coin: "BTC", time: 1763596800000, detail: { notional_usd: 1200.5, direction: "long" } }],
      },
      { tag: "junk" },
      "nope",
    ]);
    expect(tags).toHaveLength(2);
    expect(tags[0].tag).toBe("pre_move");
    expect(tags[0].evidence[0]).toMatchObject({ tid: 7, hash: HASH, coin: "BTC", time: "2025-11-20T00:00:00.000Z", notionalUsd: 1200.5, direction: "long" });
    expect(tags[1].evidence).toEqual([]);
  });
  it("is empty for missing input", () => {
    expect(readHlTags(undefined)).toEqual([]);
  });
});

describe("hlCurve", () => {
  it("maps [iso, decimal-string] pairs to PnL points and drops junk", () => {
    expect(
      hlCurve([
        ["2026-11-01T00:00:00+00:00", "12.5"],
        ["bad"],
        ["2026-11-02T00:00:00+00:00", "-3"],
      ]),
    ).toEqual([
      { t: "2026-11-01T00:00:00+00:00", cum_pnl_sol: 12.5 },
      { t: "2026-11-02T00:00:00+00:00", cum_pnl_sol: -3 },
    ]);
    expect(hlCurve(null)).toEqual([]);
  });
});

describe("num", () => {
  it("parses JSON decimals (strings) and numbers", () => {
    expect(num("1.5")).toBe(1.5);
    expect(num(2)).toBe(2);
    expect(num("x")).toBeNull();
    expect(num(null)).toBeNull();
  });
});
