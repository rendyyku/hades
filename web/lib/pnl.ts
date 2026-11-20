import type { PnlPoint } from "./api";

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
