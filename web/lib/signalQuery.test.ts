import { describe, expect, it } from "vitest";
import type { SignalRow } from "./api";
import { matchesQuery, parseSignalQuery, signalsApiPath } from "./signalQuery";

describe("parseSignalQuery", () => {
  it("keeps valid filters and drops junk", () => {
    expect(parseSignalQuery({ source: "ct", chain: "monad", min_score: "40", buyable: "false" })).toEqual({
      source: "ct",
      chain: "monad",
      minScore: 40,
      buyable: false,
    });
    expect(parseSignalQuery({ source: "evil", chain: "a b", min_score: "999", buyable: "maybe" })).toEqual({
      source: null,
      chain: null,
      minScore: null,
      buyable: null,
    });
    expect(parseSignalQuery({ source: ["ct", "repo_diff"] }).source).toBe("ct");
  });
});

describe("signalsApiPath", () => {
  it("builds the engine path", () => {
    expect(signalsApiPath({ source: "ct", chain: null, minScore: 40, buyable: false })).toBe(
      "/api/signals?source=ct&min_score=40&buyable=false&limit=50",
    );
    expect(signalsApiPath({ source: null, chain: null, minScore: null, buyable: null }, 20)).toBe(
      "/api/signals?limit=20",
    );
  });
});

describe("matchesQuery", () => {
  const r = { source: "ct", chain: "monad", score: 50, enrichment: { buyable: false } } as unknown as SignalRow;
  it("filters live events like the engine does", () => {
    expect(matchesQuery(r, { source: "ct", chain: "monad", minScore: 40, buyable: false })).toBe(true);
    expect(matchesQuery(r, { source: "repo_diff", chain: null, minScore: null, buyable: null })).toBe(false);
    expect(matchesQuery(r, { source: null, chain: null, minScore: 60, buyable: null })).toBe(false);
    expect(matchesQuery(r, { source: null, chain: null, minScore: null, buyable: true })).toBe(false);
  });
});
