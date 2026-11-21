"use client";

import {
  BaselineSeries,
  createChart,
  LastPriceAnimationMode,
  LineSeries,
  type LineData,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useId, useMemo, useRef } from "react";
import type { PnlPoint } from "@/lib/api";
import { drawdownCurve } from "@/lib/arena";
import { buildChartOptions, CHART_HEIGHT_PX } from "@/lib/chartOptions";
import { formatPct, formatUsd } from "@/lib/format";
import { toChartPoints } from "@/lib/pnl";

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** `#rrggbb` token → rgba() with alpha (falls back to the token itself). */
function alpha(hex: string, a: number): string {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  return m ? `rgba(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}, ${a})` : hex;
}

/**
 * Our paper equity (USD) with the drawdown from the running peak shaded underneath
 * (overlay scale, %). The last point is the live "now" mark: display-only (P54).
 */
export function EquityCurve({ curve }: { curve: PnlPoint[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const captionId = useId();
  const points = useMemo(() => toChartPoints(curve), [curve]);
  const dd = useMemo(
    () => toChartPoints(drawdownCurve(curve)).map((p) => ({ time: p.time, value: p.value * 100 })),
    [curve],
  );
  const first = points.length ? points[0].value : 0;
  const last = points.length ? points[points.length - 1].value : 0;
  const worst = dd.reduce((m, p) => Math.min(m, p.value), 0);

  useEffect(() => {
    const el = ref.current;
    if (!el || points.length === 0) return;
    const muted = cssVar("--muted");
    const border = cssVar("--border");
    const loss = cssVar("--loss");
    const chart = createChart(el, buildChartOptions({ muted, border, fontFamily: getComputedStyle(el).fontFamily }));

    const shade = chart.addSeries(BaselineSeries, {
      priceScaleId: "dd",
      baseValue: { type: "price", price: 0 },
      topLineColor: "transparent",
      topFillColor1: "transparent",
      topFillColor2: "transparent",
      bottomLineColor: alpha(loss, 0.6),
      bottomFillColor1: alpha(loss, 0.08),
      bottomFillColor2: alpha(loss, 0.3),
      lineWidth: 1,
      lastValueVisible: false,
      priceLineVisible: false,
      lastPriceAnimation: LastPriceAnimationMode.Disabled,
      priceFormat: { type: "percent", precision: 1, minMove: 0.1 },
    });
    // The underwater band sits in the lower part of the pane, above the logo band.
    chart.priceScale("dd").applyOptions({ visible: false, scaleMargins: { top: 0.6, bottom: 0.2 } });
    shade.setData(dd.map((p) => ({ time: p.time as UTCTimestamp, value: p.value }) satisfies LineData));

    const line = chart.addSeries(LineSeries, {
      color: cssVar(last >= first ? "--gain" : "--loss"),
      lineWidth: 2,
      lastPriceAnimation: LastPriceAnimationMode.Disabled,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    });
    line.setData(points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value }) satisfies LineData));

    chart.timeScale().fitContent();
    return () => {
      chart.remove();
    };
  }, [points, dd, first, last]);

  if (points.length === 0) {
    return <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">No paper equity yet.</p>;
  }

  return (
    <figure aria-labelledby={captionId} className="m-0 rounded-lg border border-border bg-surface p-3 sm:p-4">
      <figcaption id={captionId} className="mb-2 text-xs text-muted">
        Paper equity over {points.length} {points.length === 1 ? "point" : "points"}, from{" "}
        {formatUsd(first, { signed: false })} to {formatUsd(last, { signed: false })}; drawdown shaded below (worst{" "}
        {formatPct(worst / 100)}). The last point is the live mark (display only).
      </figcaption>
      <div ref={ref} className="w-full font-mono" style={{ height: CHART_HEIGHT_PX }} />
    </figure>
  );
}
