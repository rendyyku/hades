import { ErrorCard } from "@/components/ErrorCard";
import { LiveFeed } from "@/components/LiveFeed";
import { apiGet, type AlertRow } from "@/lib/api";

export const dynamic = "force-dynamic";
export const metadata = { title: "Cerberus · Live — HADES" };

export default async function LivePage() {
  const res = await apiGet<AlertRow[]>("/api/alerts?limit=100");
  const initial = res.ok && Array.isArray(res.data) ? res.data : [];

  return (
    <div className="flex flex-col gap-10 pt-14 sm:gap-12 sm:pt-20">
      <header>
        <p className="mb-4 font-mono text-xs uppercase tracking-[0.2em] text-muted">
          Realtime alerts · watched wallets &amp; top 25 non-bot wallets scoring 70+
        </p>
        <h1 className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
          <span className="font-mono text-base tabular-nums text-ember">#1</span>
          <span className="font-display font-light uppercase leading-[0.9] tracking-tight text-fg text-[clamp(48px,9vw,112px)]">
            Cerberus · Live
          </span>
        </h1>
      </header>

      {!res.ok ? <ErrorCard error={res.error} /> : null}
      <LiveFeed initial={initial} />
    </div>
  );
}
