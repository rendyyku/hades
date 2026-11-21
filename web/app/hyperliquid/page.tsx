import Link from "next/link";
import { HeadOffline } from "@/components/HeadOffline";
import { HlWalletTable } from "@/components/HlWalletTable";
import { headFetch } from "@/lib/headProxy";
import type { HlWalletRow } from "@/lib/hlView";

export const dynamic = "force-dynamic";
export const metadata = { title: "Hyperliquid — HADES" };

const LIMIT = 50;
const SORTS = [
  { value: "score", label: "Score" },
  { value: "pnl", label: "Net PnL" },
] as const;
type Sort = (typeof SORTS)[number]["value"];

const PILL =
  "inline-flex min-h-11 cursor-pointer items-center rounded-full border px-4 text-sm transition-colors duration-200";
const PILL_ON = "border-fg bg-fg text-bg";
const PILL_OFF = "border-border text-muted hover:border-fg/40 hover:text-fg";

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export default async function HyperliquidPage({ searchParams }: { searchParams: SearchParams }) {
  const raw = (await searchParams).sort;
  const sort: Sort = raw === "pnl" ? "pnl" : "score";
  const res = await headFetch<HlWalletRow[]>("hyperliquid", `/api/wallets?sort=${sort}&limit=${LIMIT}`);
  const rows = res.ok && Array.isArray(res.data) ? res.data : [];

  return (
    <div className="flex flex-col gap-10 pt-14 sm:gap-12 sm:pt-20">
      <header>
        <p className="mb-4 font-mono text-xs uppercase tracking-[0.2em] text-muted">
          Hyperliquid head · perps leaderboard · bots and vaults hidden
        </p>
        <h1 className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
          <span className="font-mono text-base tabular-nums text-hl">HL</span>
          <span className="font-display font-light uppercase leading-[0.9] tracking-tight text-fg text-[clamp(48px,9vw,112px)]">
            Hyperliquid
          </span>
        </h1>
      </header>

      <nav aria-label="Leaderboard sort" className="flex flex-wrap items-center gap-3">
        <span className="w-16 text-xs uppercase tracking-[0.16em] text-muted">Sort</span>
        <ul className="flex flex-wrap gap-2">
          {SORTS.map((s) => {
            const on = sort === s.value;
            return (
              <li key={s.value}>
                <Link
                  href={s.value === "score" ? "/hyperliquid" : `/hyperliquid?sort=${s.value}`}
                  aria-current={on ? "page" : undefined}
                  className={`${PILL} ${on ? PILL_ON : PILL_OFF}`}
                >
                  {s.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      {res.ok ? (
        <HlWalletTable rows={rows} caption={`Top Hyperliquid wallets by ${sort === "pnl" ? "net PnL" : "score"}`} />
      ) : (
        <HeadOffline head="hyperliquid" status={res.offline ? undefined : res.status} />
      )}
    </div>
  );
}
