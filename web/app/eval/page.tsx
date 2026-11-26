import { ErrorCard } from "@/components/ErrorCard";
import { ScrollRegion } from "@/components/ScrollRegion";
import { Section } from "@/components/Section";
import { apiGet, type EvalReport } from "@/lib/api";
import { wib } from "@/lib/signalView";

export const dynamic = "force-dynamic";

const TH = "px-3 py-3 text-left text-xs font-normal uppercase tracking-[0.16em] text-muted";
const TD = "px-3 py-3 font-mono tabular-nums";

export default async function EvalPage() {
  const res = await apiGet<EvalReport>("/api/eval");
  return (
    <div className="flex flex-col gap-10 pt-10">
      <Section index="#1" title="Eval">
        <p className="mb-6 max-w-2xl text-sm text-muted">
          Watcher mana yang benar-benar lebih dulu dari listing, seberapa awal, dan seberapa sering salah. Hit = sinyal
          sebelum listing aset yang sama dalam jendela waktu; false positive = tidak ada listing setelah jendela habis.
          Pencocokan berdasarkan simbol (lemah). Jangan percaya watcher sebelum sampelnya cukup.
        </p>
        {!res.ok ? (
          <ErrorCard error={res.error} />
        ) : (
          <>
            <p className="mb-4 font-mono text-xs text-muted">
              Dibuat {wib(res.data.generated_at)} · jendela {res.data.window_days} hari · {res.data.misses.length} listing
              tanpa sinyal sebelumnya
            </p>
            {res.data.by_source.length ? (
              <ScrollRegion label="Hasil evaluasi">
                <table className="w-full min-w-[760px] border-collapse text-sm">
                  <thead className="border-b border-border">
                    <tr>
                      <th className={TH}>Sumber · jenis</th>
                      <th className={TH}>Sinyal</th>
                      <th className={TH}>Hit</th>
                      <th className={TH}>False positive</th>
                      <th className={TH}>Pending</th>
                      <th className={TH}>Median lead</th>
                      <th className={TH}>Min lead</th>
                      <th className={TH}>Catatan</th>
                    </tr>
                  </thead>
                  <tbody>
                    {res.data.by_source.map((r) => (
                      <tr key={`${r.source}:${r.kind}`} className="border-b border-border/60">
                        <td className="px-3 py-3 text-fg">
                          {r.source} · {r.kind}
                        </td>
                        <td className={TD}>{r.signals}</td>
                        <td className={TD}>{r.hits}</td>
                        <td className={TD}>{r.false_positives}</td>
                        <td className={TD}>{r.pending}</td>
                        <td className={TD}>{r.median_lead_h === null ? "—" : `${r.median_lead_h} j`}</td>
                        <td className={TD}>{r.min_lead_h === null ? "—" : `${r.min_lead_h} j`}</td>
                        <td className="px-3 py-3">
                          {r.small_sample ? (
                            <span className="rounded-full border border-border px-2 py-0.5 text-xs text-muted">
                              sampel kecil
                            </span>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollRegion>
            ) : (
              <p className="rounded-lg border border-border bg-surface p-6 text-muted">Belum ada sinyal untuk dinilai.</p>
            )}
          </>
        )}
      </Section>
    </div>
  );
}
