import type { PnlPoint } from "./api";

/** Empty state: the curve has one point per position with at least one sell. */
export const PNL_EMPTY_TEXT = "No positions with a sell yet";

/** Accessible caption of the PnL curve (points are positions with a sell, not closed positions). */
export function pnlCaption(n: number, finalText: string): string {
  return `Cumulative realized PnL over ${n} ${n === 1 ? "position" : "positions"} with a sell, ending at ${finalText}`;
}

export interface ChartPoint {
  time: number; // unix seconds
  value: number;
}

/** Engine PnL curve -> chart points: unix seconds, ascending, identical seconds keep the last value. */
export function toChartPoints(curve: PnlPoint[]): ChartPoint[] {
  const raw = curve
    .map((p, i) => ({ time: Math.floor(Date.parse(p.t) / 1000), value: p.cum_pnl_sol, i }))
    .filter((p) => Number.isFinite(p.time) && Number.isFinite(p.value))
    .sort((a, b) => a.time - b.time || a.i - b.i);
  const out: ChartPoint[] = [];
  for (const p of raw) {
    const last = out[out.length - 1];
    if (last && last.time === p.time) last.value = p.value;
    else out.push({ time: p.time, value: p.value });
  }
  return out;
}
