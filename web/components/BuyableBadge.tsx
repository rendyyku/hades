import { CircleCheck, CircleDashed, CircleHelp } from "lucide-react";
import type { EnrichmentRow } from "@/lib/api";
import { buyableState, formatLiquidity } from "@/lib/signalView";

const TONE = { gain: "text-gain", ember: "text-ember", primary: "text-primary", muted: "text-muted" } as const;

/** Buyability never relies on colour alone: every state has its own icon and label. */
export function BuyableBadge({ enrichment }: { enrichment: EnrichmentRow | null | undefined }) {
  const s = buyableState(enrichment);
  const Icon = s.label === "Bisa dibeli" ? CircleCheck : s.label === "Belum bisa dibeli" ? CircleDashed : CircleHelp;
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-sm">
      <Icon aria-hidden="true" className={`size-4 shrink-0 ${TONE[s.tone]}`} />
      <span className="text-fg">{s.label}</span>
      {s.label === "Bisa dibeli" ? (
        <span className="font-mono tabular-nums text-muted">{formatLiquidity(enrichment?.liquidity_usd)}</span>
      ) : null}
    </span>
  );
}
