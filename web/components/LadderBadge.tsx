/** Paper-follow ladder state (spec 7): PAPER → NOTIF → COPY. Text, never colour alone. */
const STYLE: Record<string, string> = {
  PAPER: "border-border text-muted",
  NOTIF: "border-fg/40 text-fg",
  COPY: "border-gain/60 text-gain",
};

export function LadderBadge({ ladder }: { ladder: string | null | undefined }) {
  const value = (ladder ?? "").toUpperCase() || "—";
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[11px] uppercase tracking-wide ${
        STYLE[value] ?? "border-border text-muted"
      }`}
    >
      {value}
    </span>
  );
}
