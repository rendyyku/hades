// Paper-follow / unfollow flow of FollowButton. Pure (fetch injected): vitest drives it.

import type { HeadId } from "./heads";

export type FollowAction = "follow" | "unfollow";
export type FollowIntent = "follow" | "confirm-unfollow";

/**
 * What a click on the toggle does. Following starts at once; unfollowing is never
 * sent from the toggle itself: it first asks, because it cannot be undone.
 */
export function toggleIntent(following: boolean): FollowIntent {
  return following ? "confirm-unfollow" : "follow";
}

/** Shown in the confirm step (hades_hl/follows.py: a re-follow is a NEW follow). */
export const UNFOLLOW_WARNING =
  "Stop paper-following this wallet? This cannot be undone: if you follow it again, the 28-day clock, the ladder and the paper history restart from zero, and it is never auto-followed again.";

export type FollowOutcome = { ok: true } | { ok: false; error: string };

/** POST / DELETE to the same-origin, origin-guarded proxy; the head key stays on the server. */
export async function sendFollow(
  head: HeadId,
  address: string,
  action: FollowAction,
  fetchImpl: typeof fetch = fetch,
): Promise<FollowOutcome> {
  const url = `/api/heads/${head}/follows`;
  try {
    const res =
      action === "follow"
        ? await fetchImpl(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ address }),
          })
        : await fetchImpl(`${url}?address=${encodeURIComponent(address)}`, { method: "DELETE" });
    if (res.ok) return { ok: true };
    let error = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { error?: unknown };
      if (typeof body.error === "string") error = body.error;
    } catch {
      /* keep the HTTP status */
    }
    return { ok: false, error };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "request failed" };
  }
}
