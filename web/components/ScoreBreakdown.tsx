import type { ScoreDetail } from "@/lib/api";

const ROWS: { key: keyof ScoreDetail; label: string }[] = [
  { key: "base", label: "Dasar" },
  { key: "priority_chain", label: "Chain prioritas" },
  { key: "earliness", label: "Earliness" },
  { key: "fusion", label: "Fusion" },
];

export function ScoreBreakdown({ detail }: { detail: ScoreDetail | null }) {
  if (!detail) return <p className="text-muted">Belum dinilai.</p>;
  return (
    <dl className="grid max-w-md grid-cols-[1fr_auto] gap-x-8 gap-y-2 rounded-lg border border-border bg-surface p-5 text-sm">
      {ROWS.map((r) => (
        <div key={r.key} className="contents">
          <dt className="text-muted">{r.label}</dt>
          <dd className="text-right font-mono tabular-nums text-fg">{`+${detail[r.key]}`}</dd>
        </div>
      ))}
      <dt className="border-t border-border pt-2 text-fg">Total</dt>
      <dd className="border-t border-border pt-2 text-right font-mono text-lg tabular-nums text-ember">{detail.total}</dd>
    </dl>
  );
}
