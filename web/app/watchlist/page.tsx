import { Address } from "@/components/Address";
import { ErrorCard } from "@/components/ErrorCard";
import { Section } from "@/components/Section";
import { RemoveWatchButton, WatchAddForm } from "@/components/WatchlistControls";
import { apiGet, type WatchlistRow } from "@/lib/api";
import { relTime } from "@/lib/format";

export const dynamic = "force-dynamic";
export const metadata = { title: "Watchlist — HADES" };

const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted whitespace-nowrap";
const TD = "px-3 py-3 align-middle";

export default async function WatchlistPage() {
  const res = await apiGet<WatchlistRow[]>("/api/watchlist");
  const rows = res.ok && Array.isArray(res.data) ? res.data : [];

  return (
    <div className="flex flex-col gap-14 pt-14 sm:gap-16 sm:pt-20">
      <header>
        <p className="mb-4 font-mono text-xs uppercase tracking-[0.2em] text-muted">
          Wallets Cerberus alerts on in realtime
        </p>
        <h1 className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
          <span className="font-display font-light uppercase leading-[0.9] tracking-tight text-fg text-[clamp(48px,9vw,112px)]">
            Watchlist
          </span>
          {res.ok ? (
            <span className="font-mono text-lg tabular-nums text-muted">
              {rows.length} {rows.length === 1 ? "wallet" : "wallets"}
            </span>
          ) : null}
        </h1>
      </header>

      <Section index="#1" title="Add wallet">
        <WatchAddForm />
      </Section>

      <Section index="#2" title="Watched">
        {!res.ok ? (
          <ErrorCard error={res.error} />
        ) : rows.length === 0 ? (
          <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
            No watched wallets yet. Add one above.
          </p>
        ) : (
          <div
            role="region"
            aria-label="Watched wallets (scrollable)"
            tabIndex={0}
            className="overflow-x-auto rounded-lg border border-border"
          >
            <table className="w-full min-w-[680px] border-collapse text-sm">
              <caption className="sr-only">Watched wallets, oldest first</caption>
              <thead className="border-b border-border">
                <tr>
                  <th scope="col" className={TH}>Wallet</th>
                  <th scope="col" className={TH}>Note</th>
                  <th scope="col" className={TH}>Added</th>
                  <th scope="col" className={`${TH} text-right`}>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((w) => (
                  <tr key={w.address} className="border-b border-border last:border-b-0">
                    <td className={`${TD} whitespace-nowrap`}>
                      <Address value={w.address} href={`/wallet/${w.address}`} />
                    </td>
                    <td className={`${TD} max-w-[28rem] break-words text-fg`}>
                      {w.note ? w.note : <span className="text-muted">—</span>}
                    </td>
                    <td className={`${TD} whitespace-nowrap font-mono tabular-nums text-muted`}>
                      {w.added_at ? <time dateTime={w.added_at}>{relTime(w.added_at)}</time> : "—"}
                    </td>
                    <td className={`${TD} text-right`}>
                      <RemoveWatchButton address={w.address} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}
