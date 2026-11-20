import { describe, expect, it } from "vitest";
import { PNL_EMPTY_TEXT, pnlCaption, toChartPoints } from "./pnl";

describe("toChartPoints", () => {
  it("converts ISO times to unix seconds", () => {
    expect(toChartPoints([{ t: "2026-11-19T00:00:00Z", cum_pnl_sol: 1.5 }])).toEqual([
      { time: 1795046400, value: 1.5 },
    ]);
  });
  it("sorts ascending and keeps the last value for identical timestamps", () => {
    const pts = toChartPoints([
      { t: "2026-11-19T00:00:02Z", cum_pnl_sol: 3 },
      { t: "2026-11-19T00:00:01Z", cum_pnl_sol: 1 },
      { t: "2026-11-19T00:00:01.400Z", cum_pnl_sol: 2 },
    ]);
    expect(pts.map((p) => p.value)).toEqual([2, 3]);
    expect(pts[1].time - pts[0].time).toBe(1);
  });
  it("drops invalid times and non-finite values", () => {
    expect(
      toChartPoints([
        { t: "nope", cum_pnl_sol: 1 },
        { t: "2026-11-19T00:00:00Z", cum_pnl_sol: Number.NaN },
      ]),
    ).toEqual([]);
  });
  it("returns an empty array for no points", () => {
    expect(toChartPoints([])).toEqual([]);
  });
});

describe("pnl wording", () => {
  it("counts positions with a sell, not 'closed' positions", () => {
    expect(pnlCaption(1, "+1.0000 SOL")).toBe("Cumulative realized PnL over 1 position with a sell, ending at +1.0000 SOL");
    expect(pnlCaption(3, "-2 SOL")).toBe("Cumulative realized PnL over 3 positions with a sell, ending at -2 SOL");
    expect(PNL_EMPTY_TEXT).toBe("No positions with a sell yet");
  });
});
