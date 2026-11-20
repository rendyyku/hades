"use client";

import {
  createChart,
  LastPriceAnimationMode,
  LineSeries,
  type LineData,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useId, useMemo, useRef } from "react";
import type { PnlPoint } from "@/lib/api";
import { buildChartOptions, CHART_HEIGHT_PX } from "@/lib/chartOptions";
import { formatSol } from "@/lib/format";
import { toChartPoints } from "@/lib/pnl";

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

    // Logo clearance comes from rightPriceScale.scaleMargins.bottom (see lib/chartOptions):
    // the lowest point stays above the logo band at any width, zoom or pan, so no
    // horizontal gutter is needed (and none can be lost on resize).
    const chart = createChart(el, buildChartOptions({ muted, border, fontFamily: getComputedStyle(el).fontFamily }));
    const series = chart.addSeries(LineSeries, {
      color: line,
      lineWidth: 2,
      lastPriceAnimation: LastPriceAnimationMode.Disabled,
      priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
    });
    series.setData(points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value }) satisfies LineData));

    chart.timeScale().fitContent();
    return () => {
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
      <div ref={ref} className="w-full font-mono" style={{ height: CHART_HEIGHT_PX }} />
    </figure>
  );
}
