import { ExternalLink } from "lucide-react";
import { notFound } from "next/navigation";
import { FollowButton } from "@/components/FollowButton";
import { HeadOffline } from "@/components/HeadOffline";
import { HlAddress } from "@/components/HlAddress";
import { PnlChart } from "@/components/PnlChart";
import { Section } from "@/components/Section";
import { StatCard } from "@/components/StatCard";
import { TagBadge } from "@/components/TagBadge";
import { UsdPnl } from "@/components/UsdPnl";
import { formatDuration, formatPct, formatScore, formatUsd, formatWinrate, relTime, shortAddr } from "@/lib/format";
import { headFetch } from "@/lib/headProxy";
import { getHead } from "@/lib/heads";
import { hlCurve, hlTxUrl, num, readFollow, readHlTags, type HlTag, type HlWalletRow } from "@/lib/hlView";

export const dynamic = "force-dynamic";

type Params = Promise<{ address: string }>;

export async function generateMetadata({ params }: { params: Params }) {
  const { address } = await params;
  return { title: `${shortAddr(address)} · Hyperliquid — HADES` };
}

const nf = new Intl.NumberFormat("en-US");
const x1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
const TEXT_LINK =
  "inline-flex cursor-pointer items-center gap-1 rounded font-mono text-fg underline decoration-border underline-offset-4 transition-colors duration-200 hover:decoration-fg";

function windowLabel(days: number | null): string {
  return days != null && days > 0 ? `Last ${Math.round(days)}d` : "Stats window";
}

function TagEvidence({ t }: { t: HlTag }) {
  return (
    <li className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-5">
      <div className="flex flex-wrap items-center gap-3">
        <TagBadge tag={t.tag} />
        <span className="font-mono text-xs tabular-nums text-muted">
          {t.evidence.length} {t.evidence.length === 1 ? "fill" : "fills"} of evidence
        </span>
        {t.created_at ? (
          <time dateTime={t.created_at} className="ml-auto font-mono text-xs text-muted">
            {relTime(t.created_at)}
          </time>
        ) : null}
      </div>
      {t.evidence.length ? (
        <ul aria-label={`Evidence for ${t.tag}`} className="flex flex-col gap-1 text-sm">
          {t.evidence.map((e, i) => {
            const url = hlTxUrl(e.hash);
            return (
              <li key={`${e.tid ?? ""}-${e.hash ?? ""}-${i}`} className="flex flex-wrap items-center gap-x-3">
                {url ? (
                  <a href={url} target="_blank" rel="noopener noreferrer" title={e.hash ?? undefined} className={TEXT_LINK}>
                    {shortAddr(e.hash)}
                    <ExternalLink aria-hidden="true" className="size-3.5" />
                  </a>
                ) : (
                  <span className="font-mono text-muted">no hash</span>
                )}
                <span className="font-mono text-xs text-fg">{e.coin ?? "—"}</span>
                {e.direction ? <span className="text-xs text-muted">{e.direction}</span> : null}
                {e.notionalUsd != null ? (
                  <span className="font-mono text-xs tabular-nums text-muted">
                    {formatUsd(e.notionalUsd, { signed: false })} notional
                  </span>
                ) : null}
                {e.time ? (
                  <time dateTime={e.time} className="font-mono text-xs text-muted">
                    {e.time.replace("T", " ").slice(0, 16)} UTC
                  </time>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-sm text-muted">No evidence rows recorded.</p>
      )}
    </li>
  );
}

export default async function HlWalletPage({ params }: { params: Params }) {
  const { address } = await params;
  if (!getHead("hyperliquid").address.test(address)) notFound();

  const res = await headFetch<HlWalletRow>("hyperliquid", `/api/wallets/${address}`);
  if (!res.ok && !res.offline && res.status === 404) notFound();

  const w = res.ok ? res.data : null;
  const extra = w?.extra ?? {};
  const follow = readFollow(extra.follow);
  const tags = readHlTags(extra.tags);
  const windowDays = num(extra.window_days);
  const reason = typeof extra.score_reason === "string" ? extra.score_reason : null;
  const avgLev = num(extra.avg_leverage);
  const liq = num(extra.liquidations);
  const holdS = num(extra.median_hold_s);
  const maxDd = num(extra.max_dd);

  return (
    <div className="flex flex-col gap-14 pt-14 sm:gap-16 sm:pt-20">
      <header className="flex flex-col gap-6 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <p className="mb-3 font-mono text-xs uppercase tracking-[0.2em] text-muted">
            <span className="text-hl">Hyperliquid</span> · wallet profile
          </p>
          <h1 className="mb-4 font-display text-[clamp(40px,7vw,88px)] font-light uppercase leading-[0.9] tracking-tight text-fg">
            {shortAddr(address)}
          </h1>
          <HlAddress value={address} full />
        </div>
        {w ? (
          <div className="flex flex-col items-start gap-2 sm:items-end">
            <FollowButton head="hyperliquid" address={address} initialFollowing={follow?.active === true} />
            {follow?.active ? (
              <p className="font-mono text-xs text-muted">
                {follow.ladder} · {follow.origin} · since {relTime(follow.started_at)}
              </p>
            ) : null}
          </div>
        ) : null}
      </header>

      {!w ? (
        <HeadOffline head="hyperliquid" status={!res.ok && !res.offline ? res.status : undefined} />
      ) : (
        <>
          <Section index="#1" title={`Stats · ${windowLabel(windowDays)}`}>
            {extra.history_partial === true ? (
              <p className="mb-4 text-sm text-muted">History is partial: older fills were not available.</p>
            ) : null}
            {reason ? <p className="mb-4 text-sm text-muted">{reason}</p> : null}
            <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <StatCard compact label="Score" value={formatScore(w.score)} sub="0–100" />
              <StatCard compact label="Net PnL" value={<UsdPnl value={num(w.net_pnl_usd)} align="start" />} sub="Closed PnL − fees + funding" />
              <StatCard compact label="Win rate" value={formatWinrate(w.winrate, w.n)} sub="Closed positions" />
              <StatCard compact label="Trades" value={nf.format(w.trades)} sub="Perp fills" />
              <StatCard compact label="Avg leverage" value={avgLev != null ? `${x1.format(avgLev)}x` : "—"} />
              <StatCard compact label="Liquidations" value={liq != null ? nf.format(liq) : "—"} />
              <StatCard
                compact
                label="Funding"
                value={<UsdPnl value={num(extra.funding_usd)} align="start" />}
              />
              <StatCard compact label="Max drawdown" value={formatPct(maxDd)} sub={holdS != null ? `Median hold ${formatDuration(holdS)}` : undefined} />
            </dl>
          </Section>

          <Section index="#2" title={`PnL curve · ${windowLabel(windowDays)}`}>
            <PnlChart curve={hlCurve(extra.pnl_curve)} unit="usd" />
          </Section>

          <Section index="#3" title="Tartarus · Tags">
            {tags.length === 0 ? (
              <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
                No insider tags for this wallet.
              </p>
            ) : (
              <ul className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                {tags.map((t, i) => (
                  <TagEvidence key={`${t.tag}-${i}`} t={t} />
                ))}
              </ul>
            )}
          </Section>
        </>
      )}
    </div>
  );
}
