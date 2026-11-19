/**
 * Stub until Task 12 wires the SSE stream. LIVE is only allowed when the SSE is
 * connected and the last event is < 60 s old; without a stream it is always STALE.
 */
export function LiveBadge() {
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-1 font-mono text-xs uppercase tracking-[0.16em] text-muted">
      <span aria-hidden="true" className="size-1.5 rounded-full bg-muted" />
      Stale
    </span>
  );
}
