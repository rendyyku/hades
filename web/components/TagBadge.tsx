import { tagMeta } from "@/lib/evidence";

type Props = {
  tag: string;
  /** Optional tooltip, e.g. the evidence signatures. */
  title?: string;
};

/** Insider tag pill. Styling comes from lib/evidence.tagMeta (single source). */
export function TagBadge({ tag, title }: Props) {
  const m = tagMeta(tag);
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded px-1.5 py-0.5 font-mono text-[11px] uppercase tracking-wide ${m.className}`}
    >
      {m.label}
    </span>
  );
}
