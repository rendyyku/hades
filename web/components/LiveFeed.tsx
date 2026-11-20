"use client";

import { Pause, Play } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import type { AlertRow } from "@/lib/api";
import { mergeAlerts, parseAlertEvent, parseAlertList, pausedNewLabel, shouldBackfill } from "@/lib/live";
import { AlertTable } from "./AlertTable";
import { LiveBadge } from "./LiveBadge";

const TICK_MS = 5_000;
const ANNOUNCE_DEBOUNCE_MS = 1_500;
const RETRY_MIN_MS = 2_000;
const RETRY_MAX_MS = 30_000;
const BACKFILL_URL = "/api/alerts?limit=50";

const BTN =
  "inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border px-4 text-sm transition-colors duration-200";

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/**
 * Cerberus live feed. EventSource on the same-origin proxy (/api/stream); the engine
 * key never reaches the browser. Keeps the newest 200 alerts; pause buffers new ones.
 */
export function LiveFeed({ initial }: { initial: AlertRow[] }) {
  const [alerts, setAlerts] = useState<AlertRow[]>(() => mergeAlerts([], initial));
  const [buffer, setBuffer] = useState<AlertRow[]>([]);
  // True number of alerts received while paused (the buffer itself is capped at 200).
  const [pausedCount, setPausedCount] = useState(0);
  const [paused, setPaused] = useState(false);
  const [connected, setConnected] = useState(false);
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [announcement, setAnnouncement] = useState("");

  const pausedRef = useRef(false);
  const pending = useRef(0);
  const announceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Only alerts that arrive after the first render slide in (never the server-rendered ones).
  const [animateAbove] = useState(() => initial.reduce((m, a) => Math.max(m, a.id), 0));

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

    // After a reconnect, fetch what was missed while disconnected (same-origin proxy;
    // the key stays on the server). Failures are ignored: the live stream continues.
    const backfill = async () => {
      try {
        const res = await fetch(BACKFILL_URL, { cache: "no-store" });
        if (!res.ok || disposed) return;
        const rows = parseAlertList(await res.json());
        if (disposed || rows.length === 0) return;
        if (pausedRef.current) setBuffer((b) => mergeAlerts(b, rows));
        else setAlerts((a) => mergeAlerts(a, rows));
      } catch {
        /* ignore */
      }
    };

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
        // The browser retries on its own unless the connection is CLOSED (e.g. the
        // proxy answered 502 JSON); then reconnect with exponential backoff.
        if (es && es.readyState === EventSource.CLOSED && !disposed) {
          es.close();
          retry = setTimeout(connect, backoff);
          backoff = Math.min(backoff * 2, RETRY_MAX_MS);
        }
      };
      es.addEventListener("ping", () => setLastEventAt(Date.now()));
      es.addEventListener("alert", (ev) => {
        setLastEventAt(Date.now());
        const row = parseAlertEvent((ev as MessageEvent<string>).data);
        if (!row) return;
        if (pausedRef.current) {
          setBuffer((b) => mergeAlerts(b, [row]));
          setPausedCount((n) => n + 1);
        } else setAlerts((a) => mergeAlerts(a, [row]));
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
    setAlerts((a) => mergeAlerts(a, buffer));
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

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <LiveBadge connected={connected} lastEventAt={lastEventAt} />
          <span className="font-mono text-xs tabular-nums text-muted">{plural(alerts.length, "alert")} · max 200</span>
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

      <p role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement}
      </p>

      {alerts.length === 0 ? (
        <div className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
          <p>
            No alerts yet. Cerberus alerts on swaps by{" "}
            <Link
              href="/watchlist"
              className="cursor-pointer rounded text-fg underline decoration-primary decoration-2 underline-offset-4"
            >
              watched wallets
            </Link>{" "}
            and wallets scoring 70+.
          </p>
        </div>
      ) : (
        <AlertTable rows={alerts} now={now} caption="Cerberus alerts, newest first" animateAbove={animateAbove} />
      )}
    </div>
  );
}
