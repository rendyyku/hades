import { ErrorCard } from "@/components/ErrorCard";
import { Section } from "@/components/Section";
import { SignalFeed } from "@/components/SignalFeed";
import { apiGet, type SignalRow } from "@/lib/api";
import { SOURCES, type SearchParams, parseSignalQuery, signalsApiPath } from "@/lib/signalQuery";

export const dynamic = "force-dynamic";

const FIELD =
  "min-h-11 rounded border border-border bg-bg px-3 text-sm text-fg focus-visible:outline-2 focus-visible:outline-primary";

export default async function SignalsPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const q = parseSignalQuery(await searchParams);
  const res = await apiGet<SignalRow[]>(signalsApiPath(q, 100));
  const key = JSON.stringify(q);

  return (
    <div className="flex flex-col gap-10 pt-10">
      <Section index="#1" title="Signals">
        <form method="get" className="mb-6 flex flex-wrap items-end gap-3" aria-label="Filter sinyal">
          <label className="flex flex-col gap-1 text-xs uppercase tracking-[0.16em] text-muted">
            Sumber
            <select name="source" defaultValue={q.source ?? ""} className={FIELD}>
              <option value="">Semua</option>
              {SOURCES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs uppercase tracking-[0.16em] text-muted">
            Chain
            <input name="chain" defaultValue={q.chain ?? ""} placeholder="monad" className={FIELD} />
          </label>
          <label className="flex flex-col gap-1 text-xs uppercase tracking-[0.16em] text-muted">
            Skor min
            <input name="min_score" type="number" min={0} max={100} defaultValue={q.minScore ?? ""} className={FIELD} />
          </label>
          <label className="flex flex-col gap-1 text-xs uppercase tracking-[0.16em] text-muted">
            Onchain
            <select name="buyable" defaultValue={q.buyable === null ? "" : String(q.buyable)} className={FIELD}>
              <option value="">Semua</option>
              <option value="false">Belum bisa dibeli</option>
              <option value="true">Bisa dibeli</option>
            </select>
          </label>
          <button
            type="submit"
            className="min-h-11 cursor-pointer rounded bg-primary px-5 text-sm font-medium text-fg transition-colors duration-200 hover:bg-primary/85"
          >
            Terapkan
          </button>
        </form>
        {res.ok ? <SignalFeed key={key} initial={res.data} query={q} /> : <ErrorCard error={res.error} />}
      </Section>
    </div>
  );
}
