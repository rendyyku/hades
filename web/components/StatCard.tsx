import type { ReactNode } from "react";

type Props = {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  /** Smaller value type for dense grids. */
  compact?: boolean;
};

export function StatCard({ label, value, sub, compact = false }: Props) {
  const size = compact ? "text-2xl sm:text-3xl" : "text-4xl sm:text-5xl";
  return (
    <div className="rounded-lg border border-border bg-surface p-5 backdrop-blur-md sm:p-6">
      <dt className="text-xs uppercase tracking-[0.16em] text-muted">{label}</dt>
      <dd className={`mt-3 font-mono tabular-nums text-fg ${size}`}>{value}</dd>
      {sub ? <dd className="mt-2 text-sm text-muted">{sub}</dd> : null}
    </div>
  );
}
