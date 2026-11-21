"use client";

import { useEffect, useState } from "react";
import { POSITIONS_POLL_MS, applyPoll, fetchPositions, type HlPositions, type PositionsFeed } from "@/lib/hlPositions";
import { OpenPositionsView } from "./OpenPositionsView";

type Props = {
  address: string;
  /** Positions loaded by the server render; null when the head could not serve them. */
  initial: HlPositions | null;
};

/**
 * Live open positions of a Hyperliquid wallet: polls the same-origin, Host-guarded
 * proxy (/api/heads/hyperliquid/wallets/<address>/positions) every 5 s; the head key
 * stays on the server. A hidden tab does not poll (the badge then goes stale).
 */
export function OpenPositions({ address, initial }: Props) {
  const [feed, setFeed] = useState<PositionsFeed>(() => ({
    data: initial,
    lastOkAt: initial ? Date.now() : null,
    failed: initial === null,
  }));
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function tick() {
      if (document.visibilityState !== "hidden") {
        const res = await fetchPositions(address);
        if (!alive) return;
        const t = Date.now();
        setFeed((prev) => applyPoll(prev, res, t));
      }
      if (!alive) return;
      setNow(Date.now());
      timer = setTimeout(tick, POSITIONS_POLL_MS);
    }
    timer = setTimeout(tick, POSITIONS_POLL_MS);
    return () => {
      alive = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [address]);

  return <OpenPositionsView feed={feed} now={now} />;
}
