// Pure option builder for PnlChart (lightweight-charts v5). Type-only imports, so it
// runs in the node vitest environment without touching the canvas library.

import type { ChartOptions, ColorType, DeepPartial } from "lightweight-charts";

/** Chart host height (px); matches the h-[260px] wrapper in PnlChart. */
export const CHART_HEIGHT_PX = 260;
/** Smallest pane height the bottom margin is sized for. */
export const MIN_PANE_HEIGHT_PX = 160;
/**
 * TradingView attribution logo footprint from the pane bottom: it is drawn at
 * bottom:10px and is 19px tall, so it covers the lowest ~29px of the pane.
 */
export const LOGO_BAND_PX = 29;
/** 0.2 × 160px = 32px > 29px: the lowest point always sits above the logo band. */
export const PRICE_SCALE_MARGINS = { top: 0.1, bottom: 0.2 } as const;

export interface ChartColors {
  muted: string;
  border: string;
  fontFamily: string;
}

export function buildChartOptions(colors: ChartColors): DeepPartial<ChartOptions> {
  const { muted, border, fontFamily } = colors;
  return {
    autoSize: true,
    height: CHART_HEIGHT_PX,
    layout: {
      background: { type: "solid" as ColorType.Solid, color: "transparent" },
      textColor: muted,
      fontFamily,
      fontSize: 12,
      // TradingView attribution stays ON (license); it remains a normal named link.
      attributionLogo: true,
    },
    grid: { vertLines: { color: border }, horzLines: { color: border } },
    // The bottom margin keeps the line clear of the logo band at any width or zoom.
    rightPriceScale: { borderColor: border, scaleMargins: { ...PRICE_SCALE_MARGINS } },
    // On resize keep the fitted range instead of revealing/hiding bars at the edges.
    timeScale: { borderColor: border, timeVisible: true, secondsVisible: false, lockVisibleTimeRangeOnResize: true },
    crosshair: { vertLine: { color: muted }, horzLine: { color: muted } },
    // No inertia/kinetic scrolling: nothing animates (reduced-motion safe by default).
    kineticScroll: { mouse: false, touch: false },
  };
}
