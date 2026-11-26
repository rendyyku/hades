import Image from "next/image";
import { Section } from "@/components/Section";
import { ThreadsOverlay } from "@/components/ThreadsOverlay";

// Stub until Task 13 wires the overview to the v2 engine.
export default function OverviewPage() {
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
      <Section index="#1" title="HADES · Early Signals">
        <p className="text-muted">Engine v2 sedang dibangun.</p>
      </Section>
    </div>
  );
}
