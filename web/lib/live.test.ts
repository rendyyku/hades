import { describe, expect, it } from "vitest";
import type { SignalRow } from "./api";
import { FEED_CAP, liveBadge, mergeSignal, parseSignalEvent } from "./live";

const NOW = Date.parse("2026-11-27T12:00:00Z");
const row = (id: number, p: Partial<SignalRow> = {}) => ({ id, source: "ct", kind: "ct_high", ...p }) as SignalRow;

describe("liveBadge", () => {
  it("is LIVE only when connected and the last event is < 60 s old", () => {
    expect(liveBadge(true, NOW - 10_000, NOW)).toEqual({ label: "LIVE" });
    expect(liveBadge(true, NOW - 60_000, NOW).label).toBe("STALE");
    expect(liveBadge(false, NOW - 1_000, NOW).label).toBe("STALE");
    expect(liveBadge(true, null, NOW)).toEqual({ label: "STALE", since: "never" });
  });
});

describe("parseSignalEvent", () => {
  it("accepts signal-shaped JSON only", () => {
    expect(parseSignalEvent('{"id":3,"source":"ct","kind":"ct_low"}')?.id).toBe(3);
    expect(parseSignalEvent("nope")).toBeNull();
    expect(parseSignalEvent('{"id":"3","source":"ct"}')).toBeNull();
    expect(parseSignalEvent("[1]")).toBeNull();
  });
});

describe("mergeSignal", () => {
  it("prepends new rows, replaces updated rows in place, and caps", () => {
    const list = [row(2), row(1)];
    expect(mergeSignal(list, row(3)).map((r) => r.id)).toEqual([3, 2, 1]);
    const upd = mergeSignal(list, row(1, { kind: "merged" }));
    expect(upd.map((r) => r.id)).toEqual([2, 1]);
    expect(upd[1].kind).toBe("merged");
    const big = Array.from({ length: FEED_CAP }, (_, i) => row(FEED_CAP - i));
    expect(mergeSignal(big, row(FEED_CAP + 1))).toHaveLength(FEED_CAP);
  });

  it("keeps the enrichment when an update event carries none", () => {
    const enriched = row(1, { enrichment: { buyable: true } as SignalRow["enrichment"] });
    const merged = mergeSignal([enriched], row(1, { kind: "merged" }));
    expect(merged[0].enrichment?.buyable).toBe(true);
  });
});
