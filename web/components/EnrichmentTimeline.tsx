import type { EnrichmentRow } from "@/lib/api";
import { formatLiquidity, safeHref, wib } from "@/lib/signalView";
import { BuyableBadge } from "./BuyableBadge";

/** T0 and T+24h liquidity checks, oldest first. */
export function EnrichmentTimeline({ rows }: { rows: EnrichmentRow[] }) {
  if (!rows.length) return <p className="text-muted">Belum dicek.</p>;
  return (
    <ol className="flex flex-col gap-3">
      {rows.map((e, i) => {
        const pool = safeHref(e.pool_url);
        return (
          <li key={e.id} className="rounded-lg border border-border bg-surface p-4 text-sm">
            <p className="font-mono text-xs uppercase tracking-[0.16em] text-muted">
              {i === 0 ? "T0" : `Cek ulang ${i}`} · {wib(e.checked_at)} · {e.provider}
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-x-6 gap-y-2">
              <BuyableBadge enrichment={e} />
              <span className="text-muted">
                Likuiditas <span className="font-mono text-fg">{formatLiquidity(e.liquidity_usd)}</span>
              </span>
              <span className="text-muted">
                Volume 24j <span className="font-mono text-fg">{formatLiquidity(e.volume_24h)}</span>
              </span>
              <span className="text-muted">{e.pool_count} pool</span>
              {e.dex ? <span className="text-muted">{e.dex}</span> : null}
              {pool ? (
                <a href={pool} target="_blank" rel="noopener noreferrer" className="cursor-pointer text-fg underline underline-offset-4">
                  Lihat pool
                </a>
              ) : null}
            </div>
            {!e.verified && e.candidates.length ? (
              <ul className="mt-3 flex flex-col gap-1 text-xs text-muted">
                <li>Kandidat berdasarkan simbol (belum terverifikasi):</li>
                {e.candidates.map((c, j) => {
                  const href = safeHref(c.pool_url);
                  return (
                    <li key={j} className="font-mono">
                      {c.chain} · {c.name} · {formatLiquidity(c.liquidity_usd ?? null)}{" "}
                      {href ? (
                        <a href={href} target="_blank" rel="noopener noreferrer" className="underline">
                          pool
                        </a>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            ) : null}
            {e.error ? <p className="mt-2 font-mono text-xs text-loss">{e.error}</p> : null}
          </li>
        );
      })}
    </ol>
  );
}
