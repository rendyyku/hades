"use client";

import { Pause, Play } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { HEADS, type HeadId } from "@/lib/heads";
import {
  filterFeed,
  mergeFeed,
  parseFeedEvent,
  parseHeadStatus,
  pausedNewLabel,
  shouldBackfill,
  toFeedItems,
  type FeedFilter,
  type FeedItem,
} from "@/lib/live";
import { FeedTable } from "./FeedTable";
import { LiveBadge } from "./LiveBadge";

const TICK_MS = 5_000;
const ANNOUNCE_DEBOUNCE_MS = 1_500;
const RETRY_MIN_MS = 2_000;
const RETRY_MAX_MS = 30_000;
const BACKFILL_LIMIT = 50;

const BTN =
  "inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border px-4 text-sm transition-colors duration-200";
const PILL =
  "inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-full border px-4 text-sm transition-colors duration-200";
const PILL_ON = "border-fg bg-fg text-bg";
const PILL_OFF = "border-border text-muted hover:border-fg/40 hover:text-fg";

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

const FILTERS: { value: FeedFilter; label: string }[] = [
  { value: "all", label: "All heads" },
  ...HEADS.map((h) => ({ value: h.id as FeedFilter, label: h.label })),
];

/**
 * Cerberus live feed over every head. One EventSource on the same-origin fan-in
 * (/api/stream): every event carries its `head`; head keys never reach the browser.
 * Keeps the newest 200 alerts; pause buffers new ones; the head filter only changes the view.
 */
export function LiveFeed({ initial }: { initial: FeedItem[] }) {
  const [alerts, setAlerts] = useState<FeedItem[]>(() => mergeFeed([], initial));
  const [buffer, setBuffer] = useState<FeedItem[]>([]);
  // True number of alerts received while paused (the buffer itself is capped at 200).
  const [pausedCount, setPausedCount] = useState(0);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState<FeedFilter>("all");
  const [connected, setConnected] = useState(false);
  const [headUp, setHeadUp] = useState<Partial<Record<HeadId, boolean>>>({});
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [announcement, setAnnouncement] = useState("");

  const pausedRef = useRef(false);
  const pending = useRef(0);
  const announceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Only alerts that arrive after the first render slide in (never the server-rendered ones).
  const [initialKeys] = useState<ReadonlySet<string>>(() => new Set(initial.map((i) => i.key)));

  // Relative times re-render every 5s.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(t);
  }, []);

  // Screen readers get one debounced count ("3 new alerts"), never every row.
  const announce = useCallback(() => {
    pending.current += 1;
    if (announceTimer.current) return;
    announceTimer.current = setTimeout(() => {
      const n = pending.current;
      pending.current = 0;
      announceTimer.current = null;
      setAnnouncement(`${plural(n, "new alert")}${pausedRef.current ? " (feed paused)" : ""}`);
    }, ANNOUNCE_DEBOUNCE_MS);
  }, []);

  useEffect(() => {
    let es: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let backoff = RETRY_MIN_MS;
    let disposed = false;
    let opens = 0;

    const add = (rows: FeedItem[]) => {
      if (disposed || rows.length === 0) return;
      if (pausedRef.current) setBuffer((b) => mergeFeed(b, rows));
      else setAlerts((a) => mergeFeed(a, rows));
    };

    // After a reconnect, fetch what each head emitted while disconnected (same-origin
    // proxies; keys stay on the server). Failures are ignored: the live stream continues.
    const backfill = () =>
      Promise.all(
        HEADS.map(async (h) => {
          try {
            const res = await fetch(`/api/heads/${h.id}/alerts?limit=${BACKFILL_LIMIT}`, { cache: "no-store" });
            if (!res.ok || disposed) return;
            add(toFeedItems(h.id, await res.json()));
          } catch {
            /* ignore */
          }
        }),
      );

    const connect = () => {
      es = new EventSource("/api/stream");
      es.onopen = () => {
        setConnected(true);
        backoff = RETRY_MIN_MS;
        // The first open is covered by the server-rendered list; every reopen backfills.
        if (shouldBackfill(opens)) void backfill();
        opens += 1;
      };
      es.onerror = () => {
        setConnected(false);
        setHeadUp({});
        // The browser retries on its own unless the connection is CLOSED (e.g. the
        // fan-in answered 502 JSON: no head reachable); then reconnect with backoff.
        if (es && es.readyState === EventSource.CLOSED && !disposed) {
          es.close();
          retry = setTimeout(connect, backoff);
          backoff = Math.min(backoff * 2, RETRY_MAX_MS);
        }
      };
      es.addEventListener("ping", () => setLastEventAt(Date.now()));
      es.addEventListener("head", (ev) => {
        const s = parseHeadStatus((ev as MessageEvent<string>).data);
        if (s) setHeadUp((h) => ({ ...h, [s.head]: s.up }));
      });
      es.addEventListener("alert", (ev) => {
        setLastEventAt(Date.now());
        const row = parseFeedEvent((ev as MessageEvent<string>).data);
        if (!row) return;
        if (pausedRef.current) {
          setBuffer((b) => mergeFeed(b, [row]));
          setPausedCount((n) => n + 1);
        } else setAlerts((a) => mergeFeed(a, [row]));
        announce();
      });
    };

    connect();
    return () => {
      disposed = true;
      if (retry) clearTimeout(retry);
      if (announceTimer.current) clearTimeout(announceTimer.current);
      es?.close();
    };
  }, [announce]);

  function resume() {
    pausedRef.current = false;
    setPaused(false);
    setAlerts((a) => mergeFeed(a, buffer));
    setBuffer([]);
    setPausedCount(0);
  }

  function togglePause() {
    if (paused) {
      resume();
    } else {
      pausedRef.current = true;
      setPausedCount(0);
      setPaused(true);
    }
  }

  const shown = useMemo(() => filterFeed(alerts, filter), [alerts, filter]);
  const downHeads = HEADS.filter((h) => headUp[h.id] === false);
  const filterLabel = FILTERS.find((f) => f.value === filter)?.label ?? "All heads";

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <LiveBadge connected={connected} lastEventAt={lastEventAt} />
          <span className="font-mono text-xs tabular-nums text-muted">{plural(alerts.length, "alert")} · max 200</span>
          {connected && downHeads.length ? (
            <span className="font-mono text-xs text-loss">
              {downHeads.map((h) => h.label).join(", ")} stream offline · retrying
            </span>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {paused ? (
            <button
              type="button"
              onClick={resume}
              className={`${BTN} border-ember/60 text-fg hover:border-ember`}
            >
              <span className="font-mono tabular-nums text-ember">{pausedNewLabel(pausedCount)}</span>
              <span aria-hidden="true">·</span>
              Resume
            </button>
          ) : null}
          <button
            type="button"
            onClick={togglePause}
            aria-pressed={paused}
            className={`${BTN} ${
              paused ? "border-primary bg-primary/20 text-fg hover:bg-primary/30" : "border-border bg-surface text-fg hover:border-fg/40"
            }`}
          >
            {paused ? <Play aria-hidden="true" className="size-4" /> : <Pause aria-hidden="true" className="size-4" />}
            Pause feed
          </button>
        </div>
      </div>

      <div role="group" aria-label="Filter by head" className="flex flex-wrap items-center gap-3">
        <span className="w-16 text-xs uppercase tracking-[0.16em] text-muted">Head</span>
        <ul className="flex flex-wrap gap-2">
          {FILTERS.map((f) => {
            const on = filter === f.value;
            const n = filterFeed(alerts, f.value).length;
            return (
              <li key={f.value}>
                <button type="button" aria-pressed={on} onClick={() => setFilter(f.value)} className={`${PILL} ${on ? PILL_ON : PILL_OFF}`}>
                  {f.label}
                  <span className="font-mono text-xs tabular-nums">{n}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </div>

      <p role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement}
      </p>

      {shown.length === 0 ? (
        <div className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
          {alerts.length > 0 ? (
            <p>No alerts from this head yet.</p>
          ) : (
            <p>
              No alerts yet. Cerberus alerts on swaps by{" "}
              <Link
                href="/watchlist"
                className="cursor-pointer rounded text-fg underline decoration-primary decoration-2 underline-offset-4"
              >
                watched wallets
              </Link>{" "}
              and the top 25 non-bot Solana wallets scoring 70+, and on the trades of{" "}
              <Link
                href="/arena"
                className="cursor-pointer rounded text-fg underline decoration-primary decoration-2 underline-offset-4"
              >
                paper-followed wallets
              </Link>{" "}
              that reached NOTIF or COPY.
            </p>
          )}
        </div>
      ) : (
        <FeedTable rows={shown} now={now} caption={`Cerberus alerts, ${filterLabel.toLowerCase()}, newest first`} initialKeys={initialKeys} />
      )}
    </div>
  );
}
