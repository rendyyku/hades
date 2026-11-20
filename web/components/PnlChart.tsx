"use client";

import {
  ColorType,
  createChart,
  LastPriceAnimationMode,
  LineSeries,
  type LineData,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useId, useMemo, useRef } from "react";
import type { PnlPoint } from "@/lib/api";
import { formatSol } from "@/lib/format";
import { toChartPoints } from "@/lib/pnl";

/** Space (px) kept free left of the first point so the TradingView logo never covers it. */
const LEFT_GUTTER_PX = 24;

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Cumulative realized PnL (lightweight-charts v5). Colors come from the CSS tokens at runtime. */
export function PnlChart({ curve }: { curve: PnlPoint[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const captionId = useId();
  const points = useMemo(() => toChartPoints(curve), [curve]);
  const final = points.length ? points[points.length - 1].value : 0;

  useEffect(() => {
    const el = ref.current;
    if (!el || points.length === 0) return;

    const muted = cssVar("--muted");
    const border = cssVar("--border");
    const line = cssVar(final >= 0 ? "--gain" : "--loss");

    const chart = createChart(el, {
      autoSize: true,
      height: 260,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: muted,
        fontFamily: getComputedStyle(el).fontFamily,
        fontSize: 12,
        // TradingView attribution stays ON (license); it remains a normal named link.
        attributionLogo: true,
      },
      grid: { vertLines: { color: border }, horzLines: { color: border } },
      rightPriceScale: { borderColor: border },
      timeScale: { borderColor: border, timeVisible: true, secondsVisible: false },
      crosshair: { vertLine: { color: muted }, horzLine: { color: muted } },
      // No inertia/kinetic scrolling: nothing animates (reduced-motion safe by default).
      kineticScroll: { mouse: false, touch: false },
    });
    const series = chart.addSeries(LineSeries, {
      color: line,
      lineWidth: 2,
      lastPriceAnimation: LastPriceAnimationMode.Disabled,
      priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
    });
    series.setData(points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value }) satisfies LineData));

    const ts = chart.timeScale();
    ts.fitContent();
    // Widen the visible range so ~24px stay empty left of the first point (logo gutter).
    const frame = requestAnimationFrame(() => {
      const range = ts.getVisibleLogicalRange();
      const width = ts.width();
      if (!range || width <= 0) return;
      const barsPerPx = (range.to - range.from) / width;
      ts.setVisibleLogicalRange({ from: range.from - LEFT_GUTTER_PX * barsPerPx, to: range.to });
    });
    return () => {
      cancelAnimationFrame(frame);
      chart.remove();
    };
  }, [points, final]);

  if (points.length === 0) {
    return (
      <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">No closed positions yet</p>
    );
  }

  return (
    <figure aria-labelledby={captionId} className="m-0 rounded-lg border border-border bg-surface p-3 sm:p-4">
      <figcaption id={captionId} className="sr-only">
        {`Cumulative realized PnL over ${points.length} closed ${
          points.length === 1 ? "position" : "positions"
        }, ending at ${formatSol(final)}`}
      </figcaption>
      {/* Canvas host: no role, so the TradingView attribution link inside stays a normal link. */}
      <div ref={ref} className="h-[260px] w-full font-mono" />
    </figure>
  );
}
