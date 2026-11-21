import { HeadOffline } from "@/components/HeadOffline";
import { LiveFeed } from "@/components/LiveFeed";
import { headFetch } from "@/lib/headProxy";
import { HEADS } from "@/lib/heads";
import { mergeFeed, toFeedItems } from "@/lib/live";

export const dynamic = "force-dynamic";
export const metadata = { title: "Cerberus · Live — HADES" };

export default async function LivePage() {
  // Every head's recent alerts, side by side: one head being down never blanks the others.
  const results = await Promise.all(
    HEADS.map(async (h) => ({ head: h.id, res: await headFetch<unknown>(h.id, "/api/alerts?limit=100") })),
  );
  const initial = mergeFeed(
    [],
    results.flatMap(({ head, res }) => (res.ok ? toFeedItems(head, res.data) : [])),
  );

  return (
    <div className="flex flex-col gap-10 pt-14 sm:gap-12 sm:pt-20">
      <header>
        <p className="mb-4 font-mono text-xs uppercase tracking-[0.2em] text-muted">
          Realtime alerts · every head, merged
        </p>
        <h1 className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
          <span className="font-mono text-base tabular-nums text-ember">#1</span>
          <span className="font-display font-light uppercase leading-[0.9] tracking-tight text-fg text-[clamp(48px,9vw,112px)]">
            Cerberus · Live
          </span>
        </h1>
      </header>

      {results.map(({ head, res }) =>
        res.ok ? null : <HeadOffline key={head} head={head} status={res.offline ? undefined : res.status} />,
      )}
      <LiveFeed initial={initial} />
    </div>
  );
}
