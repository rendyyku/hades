import Link from "next/link";
import { notFound } from "next/navigation";
import { Address } from "@/components/Address";
import { ErrorCard } from "@/components/ErrorCard";
import { FundingChain, TxLink } from "@/components/FundingChain";
import { PnlChart } from "@/components/PnlChart";
import { Pnl } from "@/components/Pnl";
import { PositionsTable } from "@/components/PositionsTable";
import { Section } from "@/components/Section";
import { StatCard } from "@/components/StatCard";
import { TagBadge } from "@/components/TagBadge";
import { WatchButton } from "@/components/WatchButton";
import { apiGet, type TagRow, type WalletDetail, type WatchlistRow } from "@/lib/api";
import { readEvidence } from "@/lib/evidence";
import { formatDuration, formatPct, formatScore, formatSol, relTime, shortAddr } from "@/lib/format";
import { ALL_TIME_LABEL, scoreReason, statsWindowLabel } from "@/lib/walletView";
import { BASE58_ADDRESS } from "@/lib/watchlistProxy";

export const dynamic = "force-dynamic";

type Params = Promise<{ address: string }>;

export async function generateMetadata({ params }: { params: Params }) {
  const { address } = await params;
  return { title: `${shortAddr(address)} — HADES` };
}

const nf = new Intl.NumberFormat("en-US");
const tpd = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

function TagEvidence({ t }: { t: TagRow }) {
  const ev = readEvidence(t.evidence);
  return (
    <li className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-5">
      <div className="flex flex-wrap items-center gap-3">
        <TagBadge tag={t.tag} title={ev.signatures.length ? `Evidence: ${ev.signatures.join(", ")}` : undefined} />
        {t.mint ? (
          <span className="text-sm text-muted">
            on{" "}
            <Link
              href={`/token/${t.mint}`}
              title={t.mint}
              className="cursor-pointer rounded font-mono text-fg underline decoration-border underline-offset-4 transition-colors duration-200 hover:decoration-fg"
            >
              {shortAddr(t.mint)}
            </Link>
          </span>
        ) : null}
        {ev.deltaSlots != null ? (
          <span className="font-mono text-xs tabular-nums text-muted">+{ev.deltaSlots} slots after pool</span>
        ) : null}
        {t.created_at ? (
          <time dateTime={t.created_at} className="ml-auto font-mono text-xs text-muted">
            {relTime(t.created_at)}
          </time>
        ) : null}
      </div>

      {t.tag === "bundle" && (ev.cluster || ev.funder) ? (
        <dl className="grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-1 text-sm">
          {ev.cluster ? (
            <>
              <dt className="text-xs uppercase tracking-[0.14em] text-muted">Cluster</dt>
              <dd className="font-mono text-fg">{ev.cluster}</dd>
            </>
          ) : null}
          {ev.funder ? (
            <>
              <dt className="text-xs uppercase tracking-[0.14em] text-muted">Funder</dt>
              <dd>
                <Address value={ev.funder} href={`/wallet/${ev.funder}`} />
              </dd>
            </>
          ) : null}
        </dl>
      ) : null}

      {t.tag === "deployer_link" && ev.chain.length ? (
        <div className="text-sm">
          <p className="mb-1 text-xs uppercase tracking-[0.14em] text-muted">Chain</p>
          <ol className="flex flex-wrap items-center gap-x-1 gap-y-1">
            {ev.chain.map((a, i) => (
              <li key={`${a}-${i}`} className="flex items-center gap-1">
                {i > 0 ? (
                  <span aria-hidden="true" className="text-muted">
                    →
                  </span>
                ) : null}
                <Address value={a} href={`/wallet/${a}`} />
              </li>
            ))}
          </ol>
        </div>
      ) : null}

      {ev.signatures.length ? (
        <ul aria-label={`Evidence for ${t.tag}`} className="flex flex-col gap-1 text-sm">
          {ev.signatures.map((sig, i) => (
            <li key={`${sig}-${i}`} className="flex flex-wrap items-center gap-x-3">
              <TxLink sig={sig} />
              <span className="font-mono text-xs tabular-nums text-muted">
                {ev.slots[i] != null ? `slot ${nf.format(ev.slots[i] as number)}` : "slot —"}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted">No signatures recorded.</p>
      )}
    </li>
  );
}

export default async function WalletPage({ params }: { params: Params }) {
  const { address } = await params;
  if (!BASE58_ADDRESS.test(address)) notFound();

  const [res, watch] = await Promise.all([
    apiGet<WalletDetail>(`/api/wallets/${address}`),
    apiGet<WatchlistRow[]>("/api/watchlist"),
  ]);
  if (!res.ok && res.status === 404) notFound();

  const watched = watch.ok && Array.isArray(watch.data) && watch.data.some((w) => w.address === address);
  const windowDays = res.ok ? res.data.window_days : 30;
  const reason = res.ok ? scoreReason(res.data) : null;
  const unpriced = res.ok ? (res.data.stats?.unpriced_cost_sol ?? null) : null;

  return (
    <div className="flex flex-col gap-14 pt-14 sm:gap-16 sm:pt-20">
      <header className="flex flex-col gap-6 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <p className="mb-3 font-mono text-xs uppercase tracking-[0.2em] text-muted">Wallet profile</p>
          <h1 className="mb-4 font-display text-[clamp(40px,7vw,88px)] font-light uppercase leading-[0.9] tracking-tight text-fg">
            {shortAddr(address)}
          </h1>
          <Address value={address} full />
        </div>
        <WatchButton address={address} initialWatched={watched} />
      </header>

      {!res.ok ? (
        <ErrorCard error={res.error} />
      ) : (
        <>
          <Section index="#1" title={`Stats · ${statsWindowLabel(windowDays)}`}>
            {reason ? <p className="mb-4 text-sm text-muted">{reason}</p> : null}
            <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <StatCard compact label="Score" value={formatScore(res.data.stats?.score)} sub="0–100" />
              <StatCard
                compact
                label="Total PnL"
                value={<Pnl value={res.data.stats?.total_pnl_sol ?? null} align="start" />}
              />
              <StatCard
                compact
                label="Realized"
                value={<Pnl value={res.data.stats?.realized_pnl_sol ?? null} align="start" />}
              />
              <StatCard
                compact
                label="Unrealized"
                value={<Pnl value={res.data.stats?.unrealized_pnl_sol ?? null} align="start" />}
              />
              <StatCard compact label="Win rate" value={formatPct(res.data.stats?.win_rate)} />
              <StatCard
                compact
                label="Tokens closed / traded"
                value={
                  res.data.stats
                    ? `${res.data.stats.tokens_closed ?? "—"} / ${res.data.stats.tokens_traded ?? "—"}`
                    : "—"
                }
              />
              <StatCard compact label="Median hold" value={formatDuration(res.data.stats?.median_hold_sec)} />
              <StatCard
                compact
                label="Unpriced open cost"
                value={unpriced != null ? formatSol(Math.abs(unpriced)).replace(/^\+/, "") : "—"}
                sub="Open positions with no trusted price; not in PnL or score"
              />
              <StatCard
                compact
                label="Trades / day"
                value={res.data.stats?.trades_per_day != null ? tpd.format(res.data.stats.trades_per_day) : "—"}
                sub={res.data.stats?.computed_at ? `Scored ${relTime(res.data.stats.computed_at)}` : undefined}
              />
            </dl>
          </Section>

          <Section index="#2" title={`PnL curve · ${ALL_TIME_LABEL}`}>
            <PnlChart curve={res.data.pnl_curve} />
          </Section>

          <Section index="#3" title={`Positions · ${ALL_TIME_LABEL}`}>
            <PositionsTable rows={res.data.positions} />
          </Section>

          <Section index="#4" title="Tartarus · Tags">
            {res.data.tags.length === 0 ? (
              <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
                No insider tags for this wallet.
              </p>
            ) : (
              <ul className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                {res.data.tags.map((t, i) => (
                  <TagEvidence key={`${t.tag}-${t.mint ?? ""}-${i}`} t={t} />
                ))}
              </ul>
            )}
          </Section>

          <Section index="#5" title="Styx · Funding trace">
            <FundingChain chain={res.data.funding_chain} />
          </Section>
        </>
      )}
    </div>
  );
}
