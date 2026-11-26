import Link from "next/link";
import { ErrorCard } from "@/components/ErrorCard";
import { ScrollRegion } from "@/components/ScrollRegion";
import { Section } from "@/components/Section";
import { apiGet, type ListingRow } from "@/lib/api";
import { leadBeforeListing, wib } from "@/lib/signalView";

export const dynamic = "force-dynamic";

const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.16em] text-muted";
const TD = "px-3 py-3 align-top";

export default async function ListingsPage() {
  const res = await apiGet<ListingRow[]>("/api/listings?limit=200");
  return (
    <div className="flex flex-col gap-10 pt-10">
      <Section index="#1" title="Listings">
        <p className="mb-6 max-w-2xl text-sm text-muted">
          Ground truth: kapan sebuah pair benar-benar mulai diperdagangkan di exchange, dan sinyal HADES paling awal
          untuk aset yang sama. Pencocokan berdasarkan simbol, jadi anggap sebagai petunjuk, bukan bukti.
        </p>
        {!res.ok ? (
          <ErrorCard error={res.error} />
        ) : !res.data.length ? (
          <p className="rounded-lg border border-border bg-surface p-6 text-muted">Belum ada listing baru sejak baseline.</p>
        ) : (
          <ScrollRegion label="Daftar listing">
            <table className="w-full min-w-[720px] border-collapse text-sm">
              <thead className="border-b border-border">
                <tr>
                  <th className={TH}>Waktu</th>
                  <th className={TH}>Exchange</th>
                  <th className={TH}>Pair</th>
                  <th className={TH}>Sinyal paling awal</th>
                </tr>
              </thead>
              <tbody>
                {res.data.map((l) => (
                  <tr key={l.id} className="border-b border-border/60">
                    <td className={`${TD} whitespace-nowrap font-mono tabular-nums text-muted`}>{wib(l.first_seen_at)}</td>
                    <td className={`${TD} capitalize text-fg`}>{l.exchange}</td>
                    <td className={`${TD} font-mono text-fg`}>{l.symbol}</td>
                    <td className={TD}>
                      {l.earliest_signal ? (
                        <Link
                          href={`/signal/${l.earliest_signal.id}`}
                          className="cursor-pointer text-fg underline decoration-primary decoration-2 underline-offset-4"
                        >
                          {l.earliest_signal.source} · {leadBeforeListing(l.earliest_signal.lead_h)}
                        </Link>
                      ) : (
                        <span className="text-muted">Tidak ada sinyal sebelumnya</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollRegion>
        )}
      </Section>
    </div>
  );
}
