import { describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/hl-positions.json";
import {
  POSITIONS_POLL_MS,
  applyPoll,
  fetchPositions,
  formatPx,
  formatQty,
  leverageLabel,
  positionsBadge,
  readPositions,
  type PositionsFeed,
} from "./hlPositions";

const ADDR = fixture.address;

describe("readPositions", () => {
  it("reads the head's answer (decimals as strings) into numbers", () => {
    const p = readPositions(fixture)!;
    expect(p.address).toBe(ADDR);
    expect(p.accountValue).toBeCloseTo(5437.414923, 6);
    expect(p.totalUnrealizedPnl).toBeCloseTo(-1.420472, 6);
    expect(p.asOf).toBe(fixture.as_of);
    expect(p.scope).toBe("main perp dex");
    expect(p.positions.map((x) => x.coin)).toEqual(["BTC", "ETH", "XRP"]);
    expect(p.positions[0]).toMatchObject({
      side: "short",
      size: 0.10572,
      entryPx: 60387.8,
      markPx: 60429,
      leverage: 20,
      leverageType: "isolated",
    });
    expect(p.positions[0].liquidationPx).toBeCloseTo(62630.05495065, 6);
    expect(p.positions[1]).toMatchObject({ side: "long", leverageType: "cross", liquidationPx: null });
    expect(p.positions[1].roePct).toBeCloseTo(2.33159572, 6);
  });

  it("rejects anything that is not the positions shape, and drops junk rows", () => {
    for (const bad of [null, [], "x", {}, { positions: "no" }, { address: ADDR }]) expect(readPositions(bad)).toBeNull();
    const p = readPositions({
      address: ADDR,
      positions: [null, { coin: 1 }, { coin: "BTC", side: "sideways" }, fixture.positions[0]],
    })!;
    expect(p.positions.map((x) => x.coin)).toEqual(["BTC"]);
    expect(p.accountValue).toBeNull();
  });
});

describe("formatting", () => {
  it("prices keep up to 6 significant digits", () => {
    expect(formatPx(60387.8)).toBe("$60,387.8");
    expect(formatPx(0.934985)).toBe("$0.934985");
    expect(formatPx(62630.05495065)).toBe("$62,630.1");
    expect(formatPx(null)).toBe("—");
  });

  it("sizes and leverage", () => {
    expect(formatQty(0.10572)).toBe("0.10572");
    expect(formatQty(56)).toBe("56");
    expect(formatQty(null)).toBe("—");
    expect(leverageLabel(20, "isolated")).toBe("20x isolated");
    expect(leverageLabel(3, "cross")).toBe("3x cross");
    expect(leverageLabel(null, "cross")).toBe("— cross");
  });
});

describe("poll state", () => {
  const data = readPositions(fixture)!;
  const start: PositionsFeed = { data, lastOkAt: 1_000_000, failed: false };

  it("polls every 5 s", () => {
    expect(POSITIONS_POLL_MS).toBe(5000);
  });

  it("is LIVE while polls succeed", () => {
    expect(positionsBadge(start, 1_000_000 + 4_000)).toEqual({ label: "LIVE" });
    const next = applyPoll(start, data, 1_005_000);
    expect(next).toEqual({ data, lastOkAt: 1_005_000, failed: false });
  });

  it("a failed poll keeps the last data and turns the badge STALE at once", () => {
    const failed = applyPoll(start, null, 1_005_000);
    expect(failed).toEqual({ data, lastOkAt: 1_000_000, failed: true });
    expect(positionsBadge(failed, 1_005_000)).toEqual({ label: "STALE", since: "5s ago" });
    expect(positionsBadge(applyPoll(failed, data, 1_010_000), 1_010_000)).toEqual({ label: "LIVE" });
  });

  it("goes STALE when no poll arrived for three intervals (a hidden or frozen tab)", () => {
    expect(positionsBadge(start, 1_000_000 + 3 * POSITIONS_POLL_MS)).toEqual({ label: "STALE", since: "15s ago" });
  });

  it("never loaded: offline", () => {
    const none: PositionsFeed = { data: null, lastOkAt: null, failed: true };
    expect(positionsBadge(none, 5)).toEqual({ label: "STALE", since: "never" });
    expect(applyPoll(none, null, 9)).toEqual(none);
  });
});

describe("fetchPositions", () => {
  it("GETs the same-origin proxy and reads the answer", async () => {
    const f = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture), { status: 200 }));
    const p = await fetchPositions(ADDR, f);
    expect(p?.positions).toHaveLength(3);
    expect(f.mock.calls[0][0]).toBe(`/api/heads/hyperliquid/wallets/${ADDR}/positions`);
    expect(f.mock.calls[0][1]).toMatchObject({ cache: "no-store" });
  });

  it("returns null on an HTTP error, a network error or a bad body", async () => {
    expect(await fetchPositions(ADDR, vi.fn().mockResolvedValue(new Response("{}", { status: 502 })))).toBeNull();
    expect(await fetchPositions(ADDR, vi.fn().mockRejectedValue(new Error("offline")))).toBeNull();
    expect(await fetchPositions(ADDR, vi.fn().mockResolvedValue(new Response("<html>", { status: 200 })))).toBeNull();
  });
});
