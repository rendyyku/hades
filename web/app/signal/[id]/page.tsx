import Link from "next/link";
import { notFound } from "next/navigation";
import { EnrichmentTimeline } from "@/components/EnrichmentTimeline";
import { ErrorCard } from "@/components/ErrorCard";
import { ScoreBreakdown } from "@/components/ScoreBreakdown";
import { Section } from "@/components/Section";
import { apiGet, type SignalDetail } from "@/lib/api";
import { leadText, safeHref, shortUrl, signalLabel, wib } from "@/lib/signalView";

export const dynamic = "force-dynamic";

const RAW_MAX = 2048;

export default async function SignalPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^\d{1,12}$/.test(id)) notFound();
  const res = await apiGet<SignalDetail>(`/api/signals/${id}`);
  if (!res.ok) {
    if (res.status === 404) notFound();
    return <ErrorCard error={res.error} />;
  }
  const { signal: s, enrichments, fusion } = res.data;
  const evidence = safeHref(s.evidence_url);
  const raw = JSON.stringify(s.raw ?? {}, null, 2);

  return (
    <div className="flex flex-col gap-12 pt-10">
      <header>
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-muted">
          {s.source} · {s.kind} · {s.chain ?? "—"} · {wib(s.observed_at)}
        </p>
        <h1 className="mt-3 break-all font-display text-4xl font-light uppercase tracking-tight sm:text-6xl">
          {signalLabel(s)}
        </h1>
        {s.address ? <p className="mt-3 break-all font-mono text-sm text-muted">{s.address}</p> : null}
      </header>

      <Section index="#1" title="Bukti">
        {evidence ? (
          <a
            href={evidence}
            target="_blank"
            rel="noopener noreferrer"
            className="cursor-pointer break-all font-mono text-sm text-fg underline decoration-primary decoration-2 underline-offset-4"
          >
            {shortUrl(evidence)}
          </a>
        ) : (
          <p className="text-muted">(link tidak valid)</p>
        )}
        <pre className="mt-4 max-h-80 overflow-auto rounded-lg border border-border bg-surface p-4 font-mono text-xs text-muted">
          {raw.length > RAW_MAX ? `${raw.slice(0, RAW_MAX)}\n…` : raw}
        </pre>
      </Section>

      <Section index="#2" title="Likuiditas">
        <EnrichmentTimeline rows={enrichments} />
      </Section>

      <Section index="#3" title="Skor">
        <ScoreBreakdown detail={s.score_detail} />
      </Section>

      <Section index="#4" title="Fusion">
        {fusion.length ? (
          <ul className="flex flex-col gap-2 text-sm">
            {fusion.map((f) => (
              <li key={f.id}>
                <Link href={`/signal/${f.id}`} className="cursor-pointer text-fg underline underline-offset-4">
                  {f.source} · {signalLabel(f)}
                </Link>{" "}
                <span className="text-muted">· {wib(f.observed_at)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted">Belum ada sumber lain untuk proyek ini.</p>
        )}
      </Section>

      <Section index="#5" title="Listing">
        {s.listed_on.length ? (
          <ul className="flex flex-col gap-2 text-sm text-fg">
            {s.listed_on.map((l) => {
              const hours = (Date.parse(l.at) - Date.parse(s.observed_at)) / 3600_000;
              return (
                <li key={l.exchange}>
                  Listing di <span className="capitalize">{l.exchange}</span> · {wib(l.at)} · {leadText(hours)}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-muted">Belum terlihat listing di exchange yang dipantau.</p>
        )}
      </Section>
    </div>
  );
}
