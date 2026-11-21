import Link from "next/link";
import { HeadLabel } from "@/components/HeadLabel";
import { HeadOffline } from "@/components/HeadOffline";
import { HlAddress } from "@/components/HlAddress";
import { LadderBadge } from "@/components/LadderBadge";
import { ScrollRegion } from "@/components/ScrollRegion";
import { Sparkline } from "@/components/Sparkline";
import { UsdPnl } from "@/components/UsdPnl";
import { ARENA_MIN_CLOSED, arenaRows, followsToEntries, type ArenaRow, type HeadState } from "@/lib/arena";
import { formatPct, formatUsd, shortAddr } from "@/lib/format";
import { headFetch } from "@/lib/headProxy";
import { getHead, HEADS, type HeadId } from "@/lib/heads";

export const dynamic = "force-dynamic";
export const metadata = { title: "Paper Arena — HADES" };

const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted whitespace-nowrap";
const TD = "px-3 py-3 align-middle whitespace-nowrap";

async function headState(head: HeadId): Promise<HeadState> {
  const res = await headFetch<unknown>(head, "/api/follows");
  if (res.ok) return followsToEntries(res.data);
  if (res.offline) return "offline";
  // A head without the paper routes yet (Solana in Phase 2a) answers 404: not available.
  return res.status === 404 ? "unavailable" : { status: res.status };
}

function WalletLink({ row }: { row: Extract<ArenaRow, { kind: "wallet" }> }) {
  const href = `/arena/${row.head}/${row.address}`;
  if (row.head === "hyperliquid") return <HlAddress value={row.address} href={href} />;
  return (
    <Link href={href} title={row.address} className="cursor-pointer rounded font-mono text-fg underline decoration-border underline-offset-4 hover:decoration-fg">
      {shortAddr(row.address)}
    </Link>
  );
}

export default async function ArenaPage() {
  const states = await Promise.all(HEADS.map(async (h) => [h.id, await headState(h.id)] as const));
  const rows = arenaRows(Object.fromEntries(states));
  const wallets = rows.filter((r): r is Extract<ArenaRow, { kind: "wallet" }> => r.kind === "wallet");
  const down = rows.filter((r): r is Extract<ArenaRow, { kind: "head" }> => r.kind === "head");

  return (
    <div className="flex flex-col gap-10 pt-14 sm:gap-12 sm:pt-20">
      <header>
        <p className="mb-4 font-mono text-xs uppercase tracking-[0.2em] text-muted">
          Followed wallets across heads · ranked by our net paper PnL
        </p>
        <h1 className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
          <span className="font-mono text-base tabular-nums text-ember">#P</span>
          <span className="font-display font-light uppercase leading-[0.9] tracking-tight text-fg text-[clamp(48px,9vw,112px)]">
            Paper Arena
          </span>
        </h1>
      </header>

      {wallets.length === 0 ? (
        <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
          No followed wallets yet. Follow a wallet from a head&apos;s wallet page to start paper trading it.
        </p>
      ) : (
        <ScrollRegion label="Paper Arena ranking (scrollable)" className="rounded-lg border border-border">
          <table className="w-full min-w-[980px] border-collapse text-sm">
            <caption className="sr-only">Followed wallets ranked by our net paper PnL, highest first</caption>
            <thead className="border-b border-border">
              <tr>
                <th scope="col" className={`${TH} text-right`}>#</th>
                <th scope="col" className={TH}>Head</th>
                <th scope="col" className={TH}>Wallet</th>
                <th scope="col" className={TH}>Ladder</th>
                <th scope="col" className={`${TH} text-right`}>Net paper PnL</th>
                <th scope="col" className={`${TH} text-right`}>Max DD</th>
                <th scope="col" className={`${TH} text-right`}>Days</th>
                <th scope="col" className={`${TH} text-right`}>Closed / {ARENA_MIN_CLOSED}</th>
                <th scope="col" className={`${TH} text-right`}>Leader gap</th>
                <th scope="col" className={TH}>Equity</th>
              </tr>
            </thead>
            <tbody>
              {wallets.map((r, i) => {
                return (
                  <tr key={`${r.head}:${r.address}`} className="border-b border-border last:border-b-0 hover:bg-white/[0.03]">
                    <td className={`${TD} text-right font-mono tabular-nums text-muted`}>{i + 1}</td>
                    <td className={TD}>
                      <HeadLabel head={r.head} />
                    </td>
                    <td className={TD}>
                      <WalletLink row={r} />
                      {r.origin === "manual" ? <span className="ml-2 text-xs text-muted">manual</span> : null}
                    </td>
                    <td className={TD}>
                      <LadderBadge ladder={r.ladder} />
                    </td>
                    <td className={`${TD} text-right font-mono tabular-nums`}>
                      {r.netPnl == null ? <span className="text-muted">no paper result yet</span> : <UsdPnl value={r.netPnl} />}
                    </td>
                    <td className={`${TD} text-right font-mono tabular-nums text-fg`}>{formatPct(r.maxDd)}</td>
                    <td className={`${TD} text-right font-mono tabular-nums text-fg`}>{r.days ?? "—"}</td>
                    <td className={`${TD} text-right font-mono tabular-nums text-fg`}>
                      {r.closed ?? "—"} / {ARENA_MIN_CLOSED}
                    </td>
                    <td className={`${TD} text-right font-mono tabular-nums text-fg`}>{formatUsd(r.gap)}</td>
                    <td className={TD}>
                      <Sparkline values={r.sparkline} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </ScrollRegion>
      )}

      {down.length ? (
        <section aria-label="Heads without a paper ranking" className="flex flex-col gap-3">
          {down.map((r) =>
            r.state === "unavailable" ? (
              <p key={r.head} className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
                {getHead(r.head).label}: paper following is not available on this head yet.
              </p>
            ) : (
              <HeadOffline key={r.head} head={r.head} status={r.state === "error" ? r.status : undefined} />
            ),
          )}
        </section>
      ) : null}
    </div>
  );
}
