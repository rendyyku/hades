import { ChevronLeft, ChevronRight } from "lucide-react";
import Link from "next/link";
import { ErrorCard } from "@/components/ErrorCard";
import { WalletTable } from "@/components/WalletTable";
import { apiGet, type WalletList } from "@/lib/api";
import { tagMeta } from "@/lib/evidence";
import { parseWalletQuery, TAGS, walletsApiPath, walletsHref, type SearchParams, type WalletSort } from "@/lib/query";

export const dynamic = "force-dynamic";
export const metadata = { title: "Judgement — HADES" };

const PAGE_SIZE = 50;
const nf = new Intl.NumberFormat("en-US");

const SORT_LABELS: { value: WalletSort; label: string }[] = [
  { value: "score", label: "Score" },
  { value: "total_pnl_sol", label: "PnL" },
  { value: "win_rate", label: "Win rate" },
];

const PILL =
  "inline-flex min-h-11 cursor-pointer items-center rounded-full border px-4 text-sm transition-colors duration-200";
const PILL_ON = "border-fg bg-fg text-bg";
const PILL_OFF = "border-border text-muted hover:border-fg/40 hover:text-fg";
const PAGER =
  "inline-flex min-h-11 cursor-pointer items-center gap-1 rounded-lg border border-border px-3 text-sm text-fg transition-colors duration-200 hover:border-fg/40";
const PAGER_OFF = "inline-flex min-h-11 items-center gap-1 rounded-lg border border-border px-3 text-sm text-muted opacity-60";

export default async function WalletsPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const q = parseWalletQuery(await searchParams);
  const res = await apiGet<WalletList>(walletsApiPath(q, PAGE_SIZE));

  const total = res.ok ? res.data.total : 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="flex flex-col gap-10 pt-14 sm:gap-12 sm:pt-20">
      <header>
        <p className="mb-4 font-mono text-xs uppercase tracking-[0.2em] text-muted">Leaderboard · 30-day window</p>
        <h1 className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
          <span className="font-mono text-base tabular-nums text-ember">#1</span>
          <span className="font-display font-light uppercase leading-[0.9] tracking-tight text-fg text-[clamp(48px,9vw,112px)]">
            Judgement
          </span>
          {res.ok ? (
            <span className="font-mono text-lg tabular-nums text-muted">
              {nf.format(total)} {total === 1 ? "wallet" : "wallets"}
            </span>
          ) : null}
        </h1>
      </header>

      <nav aria-label="Leaderboard filters" className="flex flex-col gap-5">
        <div className="flex flex-wrap items-center gap-3">
          <span className="w-16 text-xs uppercase tracking-[0.16em] text-muted">Sort</span>
          <ul className="flex flex-wrap gap-2">
            {SORT_LABELS.map((s) => {
              const on = q.sort === s.value;
              return (
                <li key={s.value}>
                  <Link
                    href={walletsHref(q, { sort: s.value })}
                    aria-current={on ? "page" : undefined}
                    className={`${PILL} ${on ? PILL_ON : PILL_OFF}`}
                  >
                    {s.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <span className="w-16 text-xs uppercase tracking-[0.16em] text-muted">Tag</span>
          <ul className="flex flex-wrap gap-2">
            {[null, ...TAGS].map((t) => {
              const on = q.tag === t;
              return (
                <li key={t ?? "all"}>
                  <Link
                    href={walletsHref(q, { tag: t })}
                    aria-current={on ? "page" : undefined}
                    className={`${PILL} font-mono text-xs uppercase tracking-wide ${on ? PILL_ON : PILL_OFF}`}
                  >
                    {t ? tagMeta(t).label : "All"}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <span className="w-16 text-xs uppercase tracking-[0.16em] text-muted">Bots</span>
          <Link
            href={walletsHref(q, { hideBots: !q.hideBots })}
            className={`${PILL} gap-3 ${q.hideBots ? "border-fg/40 text-fg" : PILL_OFF}`}
          >
            <span
              aria-hidden="true"
              className={`relative inline-block h-5 w-9 rounded-full border transition-colors duration-200 ${
                q.hideBots ? "border-primary bg-primary" : "border-border bg-surface"
              }`}
            >
              <span
                className={`absolute top-0.5 size-3.5 rounded-full bg-fg transition-[left] duration-200 ${
                  q.hideBots ? "left-[18px]" : "left-0.5"
                }`}
              />
            </span>
            Hide bots
            <span className="sr-only">{q.hideBots ? "(on, select to show bots)" : "(off, select to hide bots)"}</span>
          </Link>
        </div>
      </nav>

      {!res.ok ? (
        <ErrorCard error={res.error} />
      ) : (
        <div>
          <WalletTable
            rows={res.data.items}
            rankOffset={(q.page - 1) * PAGE_SIZE}
            caption={`Wallets sorted by ${SORT_LABELS.find((s) => s.value === q.sort)?.label ?? "score"}, page ${q.page} of ${totalPages}`}
            empty={
              <div className="flex flex-col gap-3">
                <p className="text-fg">No wallets match these filters.</p>
                <p>
                  {q.page > totalPages ? "This page is past the end of the list. " : "Try another tag or show bots. "}
                  <Link
                    href="/wallets"
                    className="cursor-pointer rounded text-fg underline decoration-primary decoration-2 underline-offset-4"
                  >
                    Reset filters
                  </Link>
                </p>
              </div>
            }
          />

          {total > 0 ? (
            <nav aria-label="Pagination" className="mt-6 flex items-center justify-between gap-4">
              {q.page > 1 ? (
                <Link href={walletsHref(q, { page: Math.min(q.page - 1, totalPages) })} rel="prev" className={PAGER}>
                  <ChevronLeft aria-hidden="true" className="size-4" /> Prev
                </Link>
              ) : (
                <span aria-disabled="true" className={PAGER_OFF}>
                  <ChevronLeft aria-hidden="true" className="size-4" /> Prev
                </span>
              )}
              <p className="font-mono text-sm tabular-nums text-muted" aria-current="page">
                Page {q.page} of {totalPages}
              </p>
              {q.page < totalPages ? (
                <Link href={walletsHref(q, { page: q.page + 1 })} rel="next" className={PAGER}>
                  Next <ChevronRight aria-hidden="true" className="size-4" />
                </Link>
              ) : (
                <span aria-disabled="true" className={PAGER_OFF}>
                  Next <ChevronRight aria-hidden="true" className="size-4" />
                </span>
              )}
            </nav>
          ) : null}
        </div>
      )}
    </div>
  );
}
