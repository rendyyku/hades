import { notFound } from "next/navigation";
import { EquityCurve } from "@/components/EquityCurve";
import { GateChecklist } from "@/components/GateChecklist";
import { HeadLabel } from "@/components/HeadLabel";
import { HeadOffline } from "@/components/HeadOffline";
import { HlAddress } from "@/components/HlAddress";
import { LadderBadge } from "@/components/LadderBadge";
import { PaperFillsTable } from "@/components/PaperFillsTable";
import { PaperPositionsTable } from "@/components/PaperPositionsTable";
import { Section } from "@/components/Section";
import { StatCard } from "@/components/StatCard";
import { UsdPnl } from "@/components/UsdPnl";
import {
  ARENA_MIN_CLOSED,
  equityPoints,
  formatDelayMs,
  readPaperFills,
  readPaperPositions,
  type PaperDetail,
} from "@/lib/arena";
import { formatPct, formatUsd, formatWinrate, relTime, shortAddr } from "@/lib/format";
import { headFetch } from "@/lib/headProxy";
import { getHead, isHeadId } from "@/lib/heads";
import { num } from "@/lib/hlView";

export const dynamic = "force-dynamic";

type Params = Promise<{ head: string; address: string }>;

export async function generateMetadata({ params }: { params: Params }) {
  const { address } = await params;
  return { title: `${shortAddr(address)} · Paper Arena — HADES` };
}

const nf = new Intl.NumberFormat("en-US");
const TD = "px-3 py-2 align-middle whitespace-nowrap";
const TH = "px-3 py-2 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted whitespace-nowrap";

/** A ladder change stores both gates as evaluated at that moment (paper_runtime gate_snap). */
function verdict(passed: unknown): string {
  return passed === true ? "passed" : passed === false ? "not passed" : "—";
}

export default async function PaperDetailPage({ params }: { params: Params }) {
  const { head: headParam, address } = await params;
  if (!isHeadId(headParam)) notFound();
  const head = getHead(headParam);
  if (!head.address.test(address)) notFound();

  const res = await headFetch<PaperDetail>(head.id, `/api/paper/${address}`);
  const d = res.ok && res.data && typeof res.data === "object" ? res.data : null;
  const s = d?.summary ?? null;
  const minClosed = num(d?.gate?.config?.min_closed) ?? ARENA_MIN_CLOSED;
  const history = Array.isArray(d?.ladder_history) ? d.ladder_history : [];

  return (
    <div className="flex flex-col gap-14 pt-14 sm:gap-16 sm:pt-20">
      <header className="min-w-0">
        <p className="mb-3 flex flex-wrap items-center gap-3 font-mono text-xs uppercase tracking-[0.2em] text-muted">
          <HeadLabel head={head.id} /> · paper follow
        </p>
        <h1 className="mb-4 font-display text-[clamp(40px,7vw,88px)] font-light uppercase leading-[0.9] tracking-tight text-fg">
          {shortAddr(address)}
        </h1>
        <div className="flex flex-wrap items-center gap-3">
          {head.id === "hyperliquid" ? (
            <HlAddress value={address} full href={`/hyperliquid/wallet/${address}`} />
          ) : (
            <span className="break-all font-mono text-fg">{address}</span>
          )}
          {d ? <LadderBadge ladder={s?.ladder ?? history.at(-1)?.to} /> : null}
          {d ? (
            <span className="font-mono text-xs text-muted">
              {d.active ? "following" : "unfollowed"} · since {relTime(d.follow_started_at)}
              {d.computed_at ? ` · computed ${relTime(d.computed_at)}` : ""}
            </span>
          ) : null}
        </div>
      </header>

      {!res.ok && !res.offline && res.status === 404 ? (
        <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
          No paper record for this wallet on the {head.label} head: it was never followed, or this head has no paper
          following yet.
        </p>
      ) : !d ? (
        <HeadOffline head={head.id} status={!res.ok && !res.offline ? res.status : undefined} />
      ) : (
        <>
          <Section index="#1" title="Our paper result">
            <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <StatCard compact label="Net paper PnL" value={<UsdPnl value={num(s?.net_pnl_usd)} align="start" />} sub="Equity-based, fees included" />
              <StatCard compact label="Equity" value={formatUsd(num(s?.equity_usd), { signed: false })} />
              <StatCard compact label="Max drawdown" value={formatPct(num(s?.max_dd))} />
              <StatCard compact label="Closed trades" value={`${s?.closed_trades ?? "—"} / ${minClosed}`} sub="Gate minimum" />
              <StatCard compact label="Days followed" value={s?.days_followed ?? "—"} />
              <StatCard compact label="Win rate" value={formatWinrate(d.winrate, d.n)} sub="Closed paper trades" />
            </dl>
          </Section>

          <Section index="#2" title="Equity curve">
            <EquityCurve curve={equityPoints(d.equity_curve)} />
          </Section>

          <Section index="#3" title="Gate">
            <GateChecklist gate={d.gate ?? null} />
            <p className="mt-4 text-sm text-muted">
              The gates read the persisted points only (events and hourly marks). The headline equity and the last
              curve point include the live mark, which is display-only, so they can differ by up to an hour.
            </p>
            {d.ladder_cooldown_until ? (
              <p className="mt-2 text-sm text-muted">
                Promotion cool-down: this wallet was demoted recently, so it cannot be promoted before{" "}
                <time dateTime={d.ladder_cooldown_until}>{d.ladder_cooldown_until.replace("T", " ").slice(0, 16)} UTC</time>
                , even if both gates pass.
              </p>
            ) : null}
          </Section>

          <Section index="#4" title="Leader vs paper">
            <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <StatCard compact label="Leader-vs-paper gap" value={formatUsd(num(d.gap_usd))} sub="Leader result at our size minus ours" />
              <StatCard compact label="Leader realized (raw)" value={<UsdPnl value={num(d.leader_realized_usd)} align="start" />} sub="Leader's own size, not scaled" />
              <StatCard compact label="Missed events" value={nf.format(num(d.missed_count) ?? 0)} sub="Not copied (e.g. open never seen)" />
              <StatCard compact label="Median delay" value={formatDelayMs(d.median_delay_ms)} sub="Leader fill to our observation" />
            </dl>
            {num(d.funding_missing_hours) ? (
              <p className="mt-4 text-sm text-muted">
                Funding data missing for {d.funding_missing_hours} {d.funding_missing_hours === 1 ? "hour" : "hours"}: the
                paper result may be off by that funding.
              </p>
            ) : null}
          </Section>

          <Section index="#5" title="Open paper positions">
            <PaperPositionsTable positions={readPaperPositions(d.open_positions)} />
          </Section>

          <Section index="#6" title="Paper fills">
            <PaperFillsTable fills={readPaperFills(d.fills)} missedCount={num(d.missed_count) ?? 0} />
          </Section>

          <Section index="#7" title="Ladder history">
            {history.length === 0 ? (
              <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">No ladder changes yet.</p>
            ) : (
              <div className="overflow-x-auto rounded-lg border border-border">
                <table className="w-full border-collapse text-sm">
                  <caption className="sr-only">Ladder changes, oldest first</caption>
                  <thead className="border-b border-border">
                    <tr>
                      <th scope="col" className={TH}>When</th>
                      <th scope="col" className={TH}>From</th>
                      <th scope="col" className={TH}>To</th>
                      <th scope="col" className={TH}>Gates at the change</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((h, i) => {
                      const g = (h.gate ?? {}) as { full?: { passed?: unknown }; rolling?: { passed?: unknown } };
                      return (
                        <tr key={`${h.at}-${i}`} className="border-b border-border last:border-b-0">
                          <td className={`${TD} font-mono text-xs text-muted`}>
                            <time dateTime={h.at}>{h.at.replace("T", " ").slice(0, 16)} UTC</time>
                          </td>
                          <td className={TD}>{h.from ? <LadderBadge ladder={h.from} /> : <span className="text-muted">start</span>}</td>
                          <td className={TD}>
                            <LadderBadge ladder={h.to} />
                          </td>
                          <td className={`${TD} font-mono text-xs text-muted`}>
                            full {verdict(g.full?.passed)} · rolling {verdict(g.rolling?.passed)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Section>
        </>
      )}
    </div>
  );
}
