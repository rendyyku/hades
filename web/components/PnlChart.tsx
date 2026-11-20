"use client";

import {
  ColorType,
  createChart,
  LastPriceAnimationMode,
  LineSeries,
  type LineData,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef } from "react";
import type { PnlPoint } from "@/lib/api";
import { formatSol } from "@/lib/format";
import { toChartPoints } from "@/lib/pnl";

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Cumulative realized PnL (lightweight-charts v5). Colors come from the CSS tokens at runtime. */
export function PnlChart({ curve }: { curve: PnlPoint[] }) {
  const ref = useRef<HTMLDivElement>(null);
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
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points, final]);

  if (points.length === 0) {
    return (
      <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">No closed positions yet</p>
    );
  }

  return (
    <div className="rounded-lg border border-border bg-surface p-3 sm:p-4">
      <div
        ref={ref}
        role="img"
        aria-label={`Cumulative realized PnL over ${points.length} closed ${
          points.length === 1 ? "position" : "positions"
        }, ending at ${formatSol(final)}`}
        className="h-[260px] w-full font-mono"
      />
    </div>
  );
}
