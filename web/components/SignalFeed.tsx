"use client";

import { useEffect, useRef, useState } from "react";
import type { SignalRow } from "@/lib/api";
import { mergeSignal, parseSignalEvent } from "@/lib/live";
import { type SignalQuery, matchesQuery, signalsApiPath } from "@/lib/signalQuery";
import { LiveBadge } from "./LiveBadge";
import { SignalTable } from "./SignalTable";

type Props = { initial: SignalRow[]; query: SignalQuery };

/**
 * Live feed: server-rendered rows, then SSE (`/api/stream`) for new and updated signals.
 * After a reconnect it backfills through `/api/signals` so nothing missed while offline is lost.
 */
export function SignalFeed({ initial, query }: Props) {
  const [rows, setRows] = useState(initial);
  const [connected, setConnected] = useState(false);
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const queryRef = useRef(query);
  queryRef.current = query;

  useEffect(() => {
    let hadError = false;
    const es = new EventSource("/api/stream");
    const apply = (r: SignalRow | null) => {
      if (r && matchesQuery(r, queryRef.current)) setRows((cur) => mergeSignal(cur, r));
    };
    const onSignal = (e: MessageEvent) => {
      setLastEventAt(Date.now());
      apply(parseSignalEvent(e.data));
    };
    const backfill = async () => {
      try {
        const res = await fetch(signalsApiPath(queryRef.current), { cache: "no-store" });
        if (!res.ok) return;
        const data: unknown = await res.json();
        if (Array.isArray(data)) [...data].reverse().forEach((r) => apply(r as SignalRow));
      } catch {
        /* next reconnect retries */
      }
    };
    es.onopen = () => {
      setConnected(true);
      if (hadError) void backfill();
    };
    es.onerror = () => {
      hadError = true;
      setConnected(false);
    };
    es.addEventListener("signal", onSignal);
    es.addEventListener("signal_updated", onSignal);
    es.addEventListener("ping", () => setLastEventAt(Date.now()));
    return () => es.close();
  }, []);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <LiveBadge connected={connected} lastEventAt={lastEventAt} />
      </div>
      <SignalTable rows={rows} />
    </div>
  );
}
