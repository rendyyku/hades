import { ErrorCard } from "@/components/ErrorCard";
import { Section } from "@/components/Section";
import { WatchlistControls } from "@/components/WatchlistControls";
import { apiGet, type DomainRow, type MuteRow } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function WatchlistPage() {
  const [domains, mutes] = await Promise.all([apiGet<DomainRow[]>("/api/watchlist"), apiGet<MuteRow[]>("/api/mutes")]);
  return (
    <div className="flex flex-col gap-10 pt-10">
      <Section index="#1" title="Watchlist">
        {domains.ok && mutes.ok ? (
          <WatchlistControls domains={domains.data} mutes={mutes.data} />
        ) : (
          <ErrorCard error={!domains.ok ? domains.error : !mutes.ok ? mutes.error : ""} />
        )}
      </Section>
    </div>
  );
}
