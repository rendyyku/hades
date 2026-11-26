import { ArrowRight } from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { ErrorCard } from "@/components/ErrorCard";
import { Section } from "@/components/Section";
import { SignalTable } from "@/components/SignalTable";
import { StatCard } from "@/components/StatCard";
import { ThreadsOverlay } from "@/components/ThreadsOverlay";
import { WatcherHealth } from "@/components/WatcherHealth";
import { apiGet, type Overview } from "@/lib/api";

export const dynamic = "force-dynamic";

const nf = new Intl.NumberFormat("en-US");

export default async function OverviewPage() {
  const res = await apiGet<Overview>("/api/overview");

  return (
    <div className="flex flex-col gap-16 sm:gap-20">
      <header className="grid grid-cols-1 items-center gap-10 pt-14 sm:pt-20 lg:grid-cols-2 lg:gap-12">
        <div>
          <p className="mb-4 font-mono text-xs uppercase tracking-[0.2em] text-muted">
            Early signals from public data
          </p>
          <h1 className="font-display font-light uppercase leading-[0.85] tracking-tight text-fg text-[clamp(64px,12vw,160px)]">
            HADES
          </h1>
          <p className="mt-6 max-w-xl text-lg text-fg sm:text-2xl">
            Preparation always leaves a trail<span className="text-ember">.</span>
          </p>
          <div className="mt-10 flex flex-wrap gap-4">
            <Link
              href="/signals"
              className="inline-flex min-h-11 cursor-pointer items-center gap-2 rounded bg-primary px-5 text-sm font-medium text-fg transition-colors duration-200 hover:bg-primary/85"
            >
              Buka sinyal live <ArrowRight aria-hidden="true" className="size-4" />
            </Link>
          </div>
        </div>
        <div className="hades-hero-art relative mx-auto aspect-square w-full max-w-[560px]">
          <Image
            src="/art/hades-hero.webp"
            alt="Engraving of Hades, Lord of the Many Threads, pulling taut threads in four fists"
            fill
            priority
            sizes="(min-width:1024px) 560px, 92vw"
            className="hades-hero object-cover"
          />
          <ThreadsOverlay />
        </div>
      </header>

      {!res.ok ? (
        <ErrorCard error={res.error} />
      ) : (
        <>
          <Section index="#1" title="Watchers">
            <WatcherHealth watchers={res.data.watchers} />
          </Section>
          <Section index="#2" title="24 jam">
            <dl className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <StatCard label="Sinyal" value={nf.format(res.data.counts.signals)} sub="Semua sumber" />
              <StatCard label="Alert" value={nf.format(res.data.counts.alerts)} sub="Terkirim ke Telegram" />
              <StatCard label="Listing" value={nf.format(res.data.counts.listings)} sub="Ground truth CEX" />
            </dl>
          </Section>
          <Section index="#3" title="Top sinyal">
            <SignalTable rows={res.data.top} />
          </Section>
        </>
      )}
    </div>
  );
}
