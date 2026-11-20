import { describe, expect, it } from "vitest";
import type { AlertRow } from "./api";
import { liveBadge, mergeAlerts, parseAlertEvent } from "./live";

const NOW = Date.parse("2026-11-20T12:00:00Z");

describe("liveBadge", () => {
  it("is LIVE when connected and the last event is 10s old", () => {
    expect(liveBadge(true, NOW - 10_000, NOW)).toEqual({ label: "LIVE" });
  });

  it("is STALE when connected but the last event is 90s old", () => {
    expect(liveBadge(true, NOW - 90_000, NOW)).toEqual({ label: "STALE", since: "1m ago" });
  });

  it("is STALE exactly at 60s (LIVE requires < 60s)", () => {
    expect(liveBadge(true, NOW - 60_000, NOW).label).toBe("STALE");
    expect(liveBadge(true, NOW - 59_999, NOW).label).toBe("LIVE");
  });

  it("is STALE when disconnected even with a fresh event", () => {
    expect(liveBadge(false, NOW - 1_000, NOW)).toEqual({ label: "STALE", since: "1s ago" });
  });

  it("is STALE never when no event was ever received", () => {
    expect(liveBadge(true, null, NOW)).toEqual({ label: "STALE", since: "never" });
    expect(liveBadge(false, null, NOW)).toEqual({ label: "STALE", since: "never" });
  });

  it("counts a ping as an event: a recent ping keeps it LIVE without alerts", () => {
    const lastAlert = NOW - 5 * 60_000;
    const lastPing = NOW - 15_000;
    expect(liveBadge(true, lastAlert, NOW).label).toBe("STALE");
    expect(liveBadge(true, Math.max(lastAlert, lastPing), NOW)).toEqual({ label: "LIVE" });
  });

  it("treats a clock-skewed future event as fresh", () => {
    expect(liveBadge(true, NOW + 5_000, NOW)).toEqual({ label: "LIVE" });
  });
});

function row(id: number, extra: Partial<AlertRow> = {}): AlertRow {
  return {
    id,
    wallet: "W".repeat(32),
    mint: "M".repeat(32),
    symbol: null,
    side: "buy",
    sol_amount: 1,
    signature: `sig${id}`,
    block_time: null,
    ...extra,
  };
}

describe("mergeAlerts", () => {
  it("orders newest (highest id) first", () => {
    const out = mergeAlerts([row(3), row(1)], [row(2), row(5)]);
    expect(out.map((a) => a.id)).toEqual([5, 3, 2, 1]);
  });

  it("dedupes by id, preferring the incoming row", () => {
    const out = mergeAlerts([row(2, { symbol: "OLD" }), row(1)], [row(2, { symbol: "NEW" })]);
    expect(out.map((a) => a.id)).toEqual([2, 1]);
    expect(out[0].symbol).toBe("NEW");
  });

  it("dedupes duplicates within the incoming batch", () => {
    expect(mergeAlerts([], [row(4), row(4), row(3)]).map((a) => a.id)).toEqual([4, 3]);
  });

  it("caps at 200 by default, keeping the newest", () => {
    const existing = Array.from({ length: 200 }, (_, i) => row(200 - i)); // 200..1
    const out = mergeAlerts(existing, [row(201)]);
    expect(out).toHaveLength(200);
    expect(out[0].id).toBe(201);
    expect(out[out.length - 1].id).toBe(2);
  });

  it("honours a custom cap", () => {
    expect(mergeAlerts([row(1), row(2)], [row(3)], 2).map((a) => a.id)).toEqual([3, 2]);
  });

  it("keeps nothing with a zero cap", () => {
    expect(mergeAlerts([row(1)], [row(2)], 0)).toEqual([]);
  });

  it("does not mutate its inputs", () => {
    const a = [row(1)];
    const b = [row(2)];
    mergeAlerts(a, b);
    expect(a.map((x) => x.id)).toEqual([1]);
    expect(b.map((x) => x.id)).toEqual([2]);
  });
});

describe("parseAlertEvent", () => {
  it("parses a valid SSE alert payload", () => {
    const r = row(7, { symbol: "WIF", sol_amount: 2.5, block_time: "2026-11-20T00:00:00Z" });
    expect(parseAlertEvent(JSON.stringify(r))).toEqual(r);
  });

  it("returns null for invalid JSON or a wrong shape", () => {
    expect(parseAlertEvent("{nope")).toBeNull();
    expect(parseAlertEvent("null")).toBeNull();
    expect(parseAlertEvent(JSON.stringify({ id: "1" }))).toBeNull();
    const noWallet: Partial<AlertRow> = row(1);
    delete noWallet.wallet;
    expect(parseAlertEvent(JSON.stringify(noWallet))).toBeNull();
  });

  it("normalises missing optional fields to null", () => {
    const min: Partial<AlertRow> = row(3);
    delete min.symbol;
    delete min.sol_amount;
    delete min.block_time;
    expect(parseAlertEvent(JSON.stringify(min))).toEqual({ ...row(3), symbol: null, sol_amount: null, block_time: null });
  });
});
