import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { AlertTable } from "@/components/AlertTable";
import { BadgeView } from "@/components/BadgeView";
import { ErrorCard } from "@/components/ErrorCard";
import { Section } from "@/components/Section";
import { StatCard } from "@/components/StatCard";
import { WalletTable } from "@/components/WalletTable";
import { apiGet, type AlertRow, type Overview } from "@/lib/api";
import { relTime } from "@/lib/format";

export const dynamic = "force-dynamic";

const nf = new Intl.NumberFormat("en-US");

const LINK =
  "inline-flex min-h-11 cursor-pointer items-center gap-2 rounded text-sm text-fg underline decoration-primary decoration-2 underline-offset-8 transition-colors duration-200 hover:text-muted";

export default async function OverviewPage() {
  const [res, alerts] = await Promise.all([
    apiGet<Overview>("/api/overview"),
    apiGet<AlertRow[]>("/api/alerts?limit=5"),
  ]);

  return (
    <div className="flex flex-col gap-16 sm:gap-20">
      <header className="pt-14 sm:pt-20">
        <p className="mb-4 font-mono text-xs uppercase tracking-[0.2em] text-muted">
          Solana memecoin smart-money &amp; insider intelligence
        </p>
        <h1 className="font-display font-light uppercase leading-[0.85] tracking-tight text-fg text-[clamp(64px,12vw,160px)]">
          HADES
        </h1>
        <p className="mt-6 max-w-xl text-lg text-fg sm:text-2xl">
          Smart money always leaves a trail<span className="text-ember">.</span>
        </p>
      </header>

      {!res.ok ? (
        <ErrorCard error={res.error} />
      ) : (
        <>
          <Section index="#1" title="Overview">
            <dl className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <StatCard
                label="Tracked wallets"
                value={nf.format(res.data.tracked_wallets)}
                sub={`Last swap ${relTime(res.data.last_swap_at)}`}
              />
              <StatCard label="Runner tokens" value={nf.format(res.data.runner_tokens)} sub="Tokens that ran" />
              <StatCard label="Alerts 24h" value={nf.format(res.data.alerts_24h)} sub="Cerberus, last 24 hours" />
            </dl>
          </Section>

          <Section
            index="#2"
            title="Judgement · Top 10"
            action={
              <Link href="/wallets" className={LINK}>
                View all <ArrowRight aria-hidden="true" className="size-4" />
              </Link>
            }
          >
            <WalletTable rows={res.data.top} />
          </Section>

          {/* No EventSource here: the badge is static; the live stream is on /live. */}
          <Section index="#3" title="Cerberus" action={<BadgeView state={{ label: "STALE", since: "—" }} />}>
            <div className="flex flex-col gap-4">
              <p className="text-muted">
                <span className="font-mono text-2xl tabular-nums text-fg">{nf.format(res.data.alerts_24h)}</span>{" "}
                {res.data.alerts_24h === 1 ? "alert" : "alerts"} from watched wallets in the last 24 hours.
              </p>
              {!alerts.ok ? (
                <ErrorCard error={alerts.error} />
              ) : alerts.data.length === 0 ? (
                <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">No alerts yet.</p>
              ) : (
                <AlertTable rows={alerts.data} now={Date.now()} caption="Last 5 Cerberus alerts" />
              )}
              <Link href="/live" className={`${LINK} self-start`}>
                Open live feed <ArrowRight aria-hidden="true" className="size-4" />
              </Link>
            </div>
          </Section>
        </>
      )}
    </div>
  );
}
