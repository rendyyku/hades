import type { ReactNode } from "react";

type Props = {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
};

export function StatCard({ label, value, sub }: Props) {
  return (
    <div className="rounded-lg border border-border bg-surface p-5 backdrop-blur-md sm:p-6">
      <dt className="text-xs uppercase tracking-[0.16em] text-muted">{label}</dt>
      <dd className="mt-3 font-mono text-4xl tabular-nums text-fg sm:text-5xl">{value}</dd>
      {sub ? <dd className="mt-2 text-sm text-muted">{sub}</dd> : null}
    </div>
  );
}
