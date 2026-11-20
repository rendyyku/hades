import { ExternalLink, TriangleAlert } from "lucide-react";
import { notFound } from "next/navigation";
import { Address } from "@/components/Address";
import { BundleCluster } from "@/components/BundleCluster";
import { ErrorCard } from "@/components/ErrorCard";
import { TxLink } from "@/components/FundingChain";
import { Section } from "@/components/Section";
import { StatCard } from "@/components/StatCard";
import { StyxMap } from "@/components/StyxMap";
import { TagBadge } from "@/components/TagBadge";
import { apiGet, type TokenForensics } from "@/lib/api";
import { showDeltaColumn, TRUNCATION_NOTICE } from "@/lib/forensics";
import { formatSol, relTime, shortAddr } from "@/lib/format";
import { BASE58_ADDRESS } from "@/lib/watchlistProxy";
import { ScrollRegion } from "@/components/ScrollRegion";

export const dynamic = "force-dynamic";

type Params = Promise<{ mint: string }>;

export async function generateMetadata({ params }: { params: Params }) {
  const { mint } = await params;
  return { title: `${shortAddr(mint)} · Forensics — HADES` };
}

const nf = new Intl.NumberFormat("en-US");
const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 2,
});

/** Slots after pool creation that count as sniping (Global Constraints). */
const SNIPER_DELTA = 2;

const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted whitespace-nowrap";
const TD = "px-3 py-3 align-middle whitespace-nowrap";

function sol(n: number | null): string {
  return n == null ? "—" : formatSol(Math.abs(n)).replace(/^\+/, "");
}

export default async function TokenPage({ params }: { params: Params }) {
  const { mint } = await params;
  if (!BASE58_ADDRESS.test(mint)) notFound();

  const res = await apiGet<TokenForensics>(`/api/tokens/${mint}`);
  if (!res.ok && res.status === 404) notFound();

  const t = res.ok ? res.data.token : null;
  const title = t?.symbol || t?.name || shortAddr(mint);
  const poolSlot = t?.pool_created_slot ?? null;
  const truncated = Boolean(t?.history_truncated);
  const showDelta = t ? showDeltaColumn(t) : false;

  return (
    <div className="flex flex-col gap-14 pt-14 sm:gap-16 sm:pt-20">
      <header className="flex flex-col gap-4">
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-muted">Token forensics</p>
        <h1 className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
          <span className="font-display text-[clamp(40px,7vw,88px)] font-light uppercase leading-[0.9] tracking-tight text-fg">
            {title}
          </span>
          {t?.name && t.symbol ? <span className="text-lg text-muted">{t.name}</span> : null}
        </h1>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <Address value={mint} full />
          <a
            href={`https://dexscreener.com/solana/${encodeURIComponent(mint)}`}
            target="_blank"
            rel="noopener"
            aria-label={`${title} on DexScreener (opens in a new tab)`}
            className="inline-flex min-h-11 cursor-pointer items-center gap-2 rounded text-sm text-fg underline decoration-primary decoration-2 underline-offset-8 transition-colors duration-200 hover:text-muted"
          >
            DexScreener <ExternalLink aria-hidden="true" className="size-4" />
          </a>
        </div>
      </header>

      {!res.ok || !t ? (
        <ErrorCard error={res.ok ? "invalid response" : res.error} />
      ) : (
        <>
          {truncated ? (
            <p
              role="note"
              className="flex items-start gap-3 rounded-lg border border-ember bg-surface p-5 text-sm text-fg"
            >
              <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-ember" />
              <span>{TRUNCATION_NOTICE}</span>
            </p>
          ) : null}
          <Section index="#1" title="Forensics">
            <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <StatCard
                compact
                label="Deployer"
                value={t.deployer ? <Address value={t.deployer} href={`/wallet/${t.deployer}`} /> : "—"}
                sub={!t.deployer && truncated ? "Unknown (history truncated)" : undefined}
              />
              <StatCard compact label="Pool" value={t.pool_address ? <Address value={t.pool_address} /> : "—"} />
              <StatCard
                compact
                label="Pool created"
                value={poolSlot != null ? `slot ${nf.format(poolSlot)}` : "—"}
                sub={
                  t.pool_created_at ? (
                    <time dateTime={t.pool_created_at}>{relTime(t.pool_created_at)}</time>
                  ) : truncated ? (
                    "Unknown (history truncated)"
                  ) : undefined
                }
              />
              <StatCard
                compact
                label="Runner"
                value={t.is_runner == null ? "—" : t.is_runner ? "Yes" : "No"}
                sub={t.is_runner ? "Ran per the runner rule" : undefined}
              />
              <StatCard
                compact
                label="Peak mcap"
                value={t.peak_mcap_usd != null ? usd.format(t.peak_mcap_usd) : "—"}
              />
            </dl>
          </Section>

          <Section index="#2" title={truncated ? "Earliest observed buyers" : "Early buyers"}>
            {res.data.early_buyers.length === 0 ? (
              <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
                No buys recorded for this token.
              </p>
            ) : (
              <ScrollRegion label="Early buyers (scrollable)" className="rounded-lg border border-border">
                <table className="w-full min-w-[820px] border-collapse text-sm">
                  <caption className="sr-only">
                    {showDelta ? (
                      <>
                        First {res.data.early_buyers.length} buyers by slot. Δ is slots after pool creation; Δ ≤{" "}
                        {SNIPER_DELTA} is sniper range.
                      </>
                    ) : (
                      <>
                        Earliest {res.data.early_buyers.length} observed buyers by slot. Launch slot unknown, so no
                        sniper Δ column.
                      </>
                    )}
                  </caption>
                  <thead className="border-b border-border">
                    <tr>
                      <th scope="col" className={`${TH} w-12`}>#</th>
                      <th scope="col" className={TH}>Wallet</th>
                      <th scope="col" className={`${TH} text-right`}>Slot</th>
                      {showDelta ? (
                        <th scope="col" className={`${TH} text-right`}>
                          Δ slots
                        </th>
                      ) : null}
                      <th scope="col" className={`${TH} text-right`}>SOL</th>
                      <th scope="col" className={TH}>Tx</th>
                      <th scope="col" className={TH}>Tags</th>
                    </tr>
                  </thead>
                  <tbody>
                    {res.data.early_buyers.map((b, i) => {
                      const delta = showDelta && poolSlot != null ? b.slot - poolSlot : null;
                      const sniper = delta != null && delta >= 0 && delta <= SNIPER_DELTA;
                      return (
                        <tr
                          key={b.address}
                          className="border-b border-border transition-colors duration-200 last:border-b-0 hover:bg-white/[0.03]"
                        >
                          <td className={`${TD} font-mono tabular-nums text-muted`}>{i + 1}</td>
                          <td className={TD}>
                            <Address value={b.address} href={`/wallet/${b.address}`} />
                          </td>
                          <td className={`${TD} text-right font-mono tabular-nums text-muted`}>{nf.format(b.slot)}</td>
                          {showDelta ? (
                            <td
                              className={`${TD} text-right font-mono tabular-nums ${sniper ? "text-ember" : "text-muted"}`}
                            >
                              {delta == null ? (
                                "—"
                              ) : (
                                <>
                                  {sniper ? <span className="mr-1 text-[11px]">Δ</span> : null}
                                  {delta >= 0 ? `+${nf.format(delta)}` : nf.format(delta)}
                                  {sniper ? <span className="sr-only"> (sniper range)</span> : null}
                                </>
                              )}
                            </td>
                          ) : null}
                          <td className={`${TD} text-right font-mono tabular-nums text-fg`}>{sol(b.sol_amount)}</td>
                          <td className={TD}>
                            <TxLink sig={b.signature} />
                          </td>
                          <td className={TD}>
                            <ul className="flex flex-wrap gap-1.5">
                              {b.tags.length === 0 ? <li className="text-muted">—</li> : null}
                              {b.tags.map((tag) => (
                                <li key={tag}>
                                  <TagBadge tag={tag} />
                                </li>
                              ))}
                            </ul>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </ScrollRegion>
            )}
          </Section>

          <Section index="#3" title="Tartarus · Bundles">
            {res.data.bundles.length === 0 ? (
              <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">No bundles detected</p>
            ) : (
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                {res.data.bundles.map((b, i) => (
                  <BundleCluster key={b.cluster} bundle={b} index={i} />
                ))}
              </div>
            )}
          </Section>

          <Section index="#4" title="Styx map">
            {res.data.early_buyers.length === 0 ? (
              <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
                No early buyers to map.
              </p>
            ) : (
              <div id="styx-map">
                <StyxMap data={res.data} />
              </div>
            )}
          </Section>
        </>
      )}
    </div>
  );
}
