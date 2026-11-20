import type { LiveBadgeState } from "@/lib/live";

/** Presentational LIVE / STALE pill. Server-safe; the ticking client wrapper is LiveBadge. */
export function BadgeView({ state }: { state: LiveBadgeState }) {
  const live = state.label === "LIVE";
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 font-mono text-xs uppercase tracking-[0.16em] ${
        live ? "border-gain/60 bg-gain/10 text-gain" : "border-border bg-surface text-muted"
      }`}
    >
      <span aria-hidden="true" className={`size-1.5 rounded-full ${live ? "bg-gain" : "bg-muted"}`} />
      {live ? "Live" : `Stale · ${state.since}`}
    </span>
  );
}
