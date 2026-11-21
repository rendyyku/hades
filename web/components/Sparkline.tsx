import { sparklinePath } from "@/lib/arena";

const W = 96;
const H = 24;

/** Tiny equity sparkline (decorative: the row states the numbers in text). */
export function Sparkline({ values }: { values: number[] }) {
  const d = sparklinePath(values, W, H);
  if (!d) return <span className="font-mono text-xs text-muted">—</span>;
  const up = values[values.length - 1] >= values[0];
  return (
    <svg aria-hidden="true" width={W} height={H} viewBox={`-1 -1 ${W + 2} ${H + 2}`} className="overflow-visible">
      <path d={d} fill="none" strokeWidth={1.5} className={up ? "stroke-gain" : "stroke-loss"} />
    </svg>
  );
}
