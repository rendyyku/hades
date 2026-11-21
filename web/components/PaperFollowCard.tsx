import { ArrowRight } from "lucide-react";
import Link from "next/link";
import type { PaperDetail } from "@/lib/arena";
import { formatUsd, relTime } from "@/lib/format";
import { num, type HlFollowInfo } from "@/lib/hlView";
import { LadderBadge } from "./LadderBadge";
import { StatCard } from "./StatCard";
import { UsdPnl } from "./UsdPnl";

type Props = {
  address: string;
  follow: HlFollowInfo;
  /** GET /api/paper/{address}; null when the head could not serve it. */
  paper: PaperDetail | null;
};

const LINK =
  "inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border border-border bg-surface px-4 text-sm text-fg transition-colors duration-200 hover:border-fg/40";

/**
 * What WE captured copying this wallet, next to the leader's own stats (spec 7: the
 * gap is shown wherever a followed wallet is shown), with the way to its paper page.
 */
export function PaperFollowCard({ address, follow, paper }: Props) {
  const s = paper?.summary ?? null;
  const gap = num(paper?.gap_usd);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <LadderBadge ladder={follow.ladder} />
        <span className="font-mono text-xs text-muted">
          {follow.active ? "following" : "unfollowed"} · {follow.origin}
          {follow.started_at ? ` · since ${relTime(follow.started_at)}` : ""}
        </span>
        <Link href={`/arena/hyperliquid/${address}`} className={`${LINK} sm:ml-auto`}>
          Paper fills, equity and gate
          <ArrowRight aria-hidden="true" className="size-4" />
        </Link>
      </div>
      <dl className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatCard
          compact
          label="Leader-vs-paper gap"
          value={gap != null ? formatUsd(gap) : "not available"}
          sub="Leader result at our size minus ours, copied positions only"
        />
        <StatCard
          compact
          label="Our net paper PnL"
          value={s ? <UsdPnl value={num(s.net_pnl_usd)} align="start" /> : "not available"}
          sub="What copying actually captured, fees included"
        />
        <StatCard compact label="Closed paper trades" value={s?.closed_trades ?? "—"} sub={`${paper?.missed_count ?? 0} leader events missed`} />
      </dl>
    </div>
  );
}
