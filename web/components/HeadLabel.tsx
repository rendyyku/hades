import { getHead, type Head, type HeadId } from "@/lib/heads";

/** Static class names (Tailwind only generates classes it can see in source). */
export const ACCENT_TEXT: Record<Head["accent"], string> = { ember: "text-ember", hl: "text-hl" };
export const ACCENT_BG: Record<Head["accent"], string> = { ember: "bg-ember", hl: "bg-hl" };

/** A head's name in its accent colour, with a dot (the name carries the meaning, not the colour). */
export function HeadLabel({ head }: { head: HeadId }) {
  const h = getHead(head);
  return (
    <span className={`inline-flex items-center gap-1.5 font-mono text-xs uppercase tracking-wide ${ACCENT_TEXT[h.accent]}`}>
      <span aria-hidden="true" className={`size-1.5 rounded-full ${ACCENT_BG[h.accent]}`} />
      {h.label}
    </span>
  );
}
