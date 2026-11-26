"use client";

import { useEffect, useState } from "react";
import { liveBadge } from "@/lib/live";
import { BadgeView } from "./BadgeView";

const TICK_MS = 5_000;

type Props = {
  connected: boolean;
  /** Epoch ms of the last SSE event (signal or ping), or null if none yet. */
  lastEventAt: number | null;
};

/** LIVE only when connected and the last event is < 60 s old; re-evaluated every 5 s. */
export function LiveBadge({ connected, lastEventAt }: Props) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(t);
  }, []);
  const state = liveBadge(connected, lastEventAt, Math.max(now, lastEventAt ?? 0));
  return <BadgeView state={state} />;
}
