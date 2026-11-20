import { describe, expect, it } from "vitest";
import { buildChartOptions, CHART_HEIGHT_PX, LOGO_BAND_PX, MIN_PANE_HEIGHT_PX } from "./chartOptions";

const COLORS = { muted: "#8a8f98", border: "#23262d", fontFamily: "monospace" };

describe("buildChartOptions", () => {
  const o = buildChartOptions(COLORS);

  it("keeps the visible time range when the chart is resized", () => {
    expect(o.timeScale?.lockVisibleTimeRangeOnResize).toBe(true);
  });

  it("reserves a bottom price-scale margin for the TradingView logo band", () => {
    const m = o.rightPriceScale?.scaleMargins;
    expect(m).toBeDefined();
    expect(m!.bottom).toBeGreaterThanOrEqual(0.2);
    expect(m!.top).toBeGreaterThanOrEqual(0);
    expect(m!.top! + m!.bottom!).toBeLessThan(1);
  });

  it("keeps the lowest point above the logo band at every supported pane height", () => {
    const bottom = o.rightPriceScale!.scaleMargins!.bottom!;
    // The logo sits at bottom:10px, 19px tall -> occupies the lowest ~29px of the pane.
    expect(LOGO_BAND_PX).toBeGreaterThanOrEqual(29);
    for (const h of [MIN_PANE_HEIGHT_PX, 200, CHART_HEIGHT_PX]) {
      expect(bottom * h).toBeGreaterThan(LOGO_BAND_PX);
    }
  });

  it("keeps the TradingView attribution logo on", () => {
    expect(o.layout?.attributionLogo).not.toBe(false);
    expect(o.layout?.attributionLogo).toBe(true);
  });

  it("uses autoSize, the CSS token colors and no kinetic scroll", () => {
    expect(o.autoSize).toBe(true);
    expect(o.height).toBe(CHART_HEIGHT_PX);
    expect(o.layout?.textColor).toBe(COLORS.muted);
    expect(o.layout?.fontFamily).toBe(COLORS.fontFamily);
    expect(o.grid?.vertLines?.color).toBe(COLORS.border);
    expect(o.rightPriceScale?.borderColor).toBe(COLORS.border);
    expect(o.kineticScroll).toEqual({ mouse: false, touch: false });
  });
});
