"use client";

import { useEffect, useState } from "react";
import { liveBadge } from "@/lib/live";
import { BadgeView } from "./BadgeView";

const TICK_MS = 5_000;

type Props = {
  connected: boolean;
  /** Epoch ms of the last SSE event (alert or ping), or null if none yet. */
  lastEventAt: number | null;
};

/**
 * LIVE only when the SSE is connected and the last event is < 60 s old; otherwise
 * `STALE · <relative time>`. Re-evaluated every 5 s so it decays without new events.
 * Not a live region: a status flip every few seconds would be noisy for screen readers.
 */
export function LiveBadge({ connected, lastEventAt }: Props) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(t);
  }, []);
  // A fresh event can be newer than the last tick: never compute a negative age.
  const state = liveBadge(connected, lastEventAt, Math.max(now, lastEventAt ?? 0));
  return <BadgeView state={state} />;
}
