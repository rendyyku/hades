"use client";

import { LoaderCircle, UserCheck, UserPlus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import type { HeadId } from "@/lib/heads";

type Props = {
  head: HeadId;
  address: string;
  initialFollowing: boolean;
};

/**
 * Paper-follow toggle. Talks to the same-origin, origin-guarded proxy
 * (/api/heads/<head>/follows); the head key stays on the server.
 */
export function FollowButton({ head, address, initialFollowing }: Props) {
  const router = useRouter();
  const [following, setFollowing] = useState(initialFollowing);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [, startTransition] = useTransition();

  async function toggle() {
    if (busy) return;
    const next = !following;
    setFollowing(next); // optimistic
    setError(null);
    setBusy(true);
    const url = `/api/heads/${head}/follows`;
    try {
      const res = next
        ? await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ address }),
          })
        : await fetch(`${url}?address=${encodeURIComponent(address)}`, { method: "DELETE" });
      if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        try {
          const body = (await res.json()) as { error?: unknown };
          if (typeof body.error === "string") msg = body.error;
        } catch {
          /* keep HTTP status */
        }
        throw new Error(msg);
      }
      startTransition(() => router.refresh());
    } catch (e) {
      setFollowing(!next); // roll back
      setError(e instanceof Error ? e.message : "request failed");
    } finally {
      setBusy(false);
    }
  }

  const Icon = busy ? LoaderCircle : following ? UserCheck : UserPlus;
  return (
    <div className="flex flex-col items-start gap-2 sm:items-end">
      <button
        type="button"
        onClick={toggle}
        aria-pressed={following}
        aria-busy={busy}
        disabled={busy}
        className={`inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border px-4 text-sm transition-colors duration-200 disabled:cursor-wait ${
          following
            ? "border-hl bg-hl/20 text-fg hover:bg-hl/30"
            : "border-border bg-surface text-fg hover:border-fg/40"
        }`}
      >
        <Icon aria-hidden="true" className={`size-4 ${busy ? "motion-safe:animate-spin" : ""}`} />
        {following ? "Paper-following" : "Paper-follow"}
      </button>
      <p role="status" aria-live="polite" className={error ? "text-sm text-loss" : "sr-only"}>
        {error ? `Could not update follow · ${error}` : ""}
      </p>
    </div>
  );
}
