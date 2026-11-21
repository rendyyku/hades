import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { GateStatus } from "@/lib/arena";
import { GateChecklist } from "./GateChecklist";

const gate: GateStatus = {
  full: {
    passed: false,
    checks: { days: false, closed_trades: true, net_pnl: true, max_dd: true, top_share: true },
    values: { days: "3", closed_trades: "31", net_pnl: "10", realized_net_pnl: "8", max_dd: "0.02", top_share: "0.3" },
  },
  rolling: {
    passed: true,
    checks: { net_pnl: true, max_dd: true },
    values: { window_days: "28", net_pnl: "10", max_dd: "0.02", closed_trades: "31" },
  },
  config: { min_days: 28, min_closed: 30, max_dd: "0.2", max_top_share: "0.5", rolling_days: 28 },
};

describe("GateChecklist", () => {
  const html = renderToStaticMarkup(<GateChecklist gate={gate} />);

  it("shows each check's value and threshold for the full gate", () => {
    expect(html).toContain("3 d");
    expect(html).toContain("≥ 28 d");
    expect(html).toContain("31");
    expect(html).toContain("≥ 30");
    expect(html).toContain("&lt; 20.0%");
    expect(html).toContain("≤ 50.0% of net profit");
    expect(html).toContain("30.0%");
  });

  it("shows both the full and the rolling gate", () => {
    expect(html).toContain("Full gate");
    expect(html).toContain("Rolling gate");
    expect(html).toContain("Net paper PnL (last 28 d)");
  });

  it("states pass/fail in text, not only colour", () => {
    expect(html).toContain("Fail");
    expect(html).toContain("Pass");
  });

  it("renders a not-yet-evaluated gate without crashing", () => {
    expect(renderToStaticMarkup(<GateChecklist gate={null} />)).toContain("not evaluated yet");
  });
});
