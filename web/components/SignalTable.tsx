import Link from "next/link";
import type { SignalRow } from "@/lib/api";
import { safeHref, scoreTone, shortUrl, signalLabel, wib } from "@/lib/signalView";
import { BuyableBadge } from "./BuyableBadge";
import { ScrollRegion } from "./ScrollRegion";

const TONE = { ember: "text-ember", primary: "text-primary", muted: "text-muted", gain: "text-gain" } as const;
const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.16em] text-muted";
const TD = "px-3 py-3 align-top";

export function SignalTable({ rows }: { rows: SignalRow[] }) {
  if (!rows.length) {
    return <p className="rounded-lg border border-border bg-surface p-6 text-muted">Belum ada sinyal.</p>;
  }
  return (
    <ScrollRegion label="Daftar sinyal">
      <table className="w-full min-w-[860px] border-collapse text-sm">
        <thead className="border-b border-border">
          <tr>
            <th className={TH}>Waktu</th>
            <th className={TH}>Skor</th>
            <th className={TH}>Sumber</th>
            <th className={TH}>Chain</th>
            <th className={TH}>Token / subdomain</th>
            <th className={TH}>Onchain</th>
            <th className={TH}>Bukti</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const href = safeHref(r.evidence_url);
            return (
              <tr key={r.id} className="border-b border-border/60 hover:bg-surface">
                <td className={`${TD} whitespace-nowrap font-mono tabular-nums text-muted`}>{wib(r.observed_at)}</td>
                <td className={`${TD} font-mono tabular-nums ${TONE[scoreTone(r.score)]}`}>{r.score ?? "—"}</td>
                <td className={`${TD} whitespace-nowrap text-fg`}>
                  {r.source} · {r.kind}
                  {r.severity === "low" ? <span className="ml-2 text-xs text-muted">(lemah)</span> : null}
                </td>
                <td className={`${TD} text-muted`}>{r.chain ?? "—"}</td>
                <td className={TD}>
                  <Link
                    href={`/signal/${r.id}`}
                    className="cursor-pointer text-fg underline decoration-primary decoration-2 underline-offset-4 hover:text-muted"
                  >
                    {signalLabel(r)}
                  </Link>
                </td>
                <td className={TD}>
                  <BuyableBadge enrichment={r.enrichment} />
                </td>
                <td className={`${TD} max-w-[260px] truncate`}>
                  {href ? (
                    <a
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="cursor-pointer font-mono text-xs text-muted underline underline-offset-4 hover:text-fg"
                    >
                      {shortUrl(href)}
                    </a>
                  ) : (
                    <span className="font-mono text-xs text-muted">(link tidak valid)</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </ScrollRegion>
  );
}
