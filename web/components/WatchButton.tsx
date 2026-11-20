"use client";

import { Eye, EyeOff, LoaderCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

type Props = {
  address: string;
  initialWatched: boolean;
};

/** Watch toggle. Talks to the same-origin proxy (/api/watchlist); the key stays on the server. */
export function WatchButton({ address, initialWatched }: Props) {
  const router = useRouter();
  const [watched, setWatched] = useState(initialWatched);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [, startTransition] = useTransition();

  async function toggle() {
    if (busy) return;
    const next = !watched;
    setWatched(next); // optimistic
    setError(null);
    setBusy(true);
    try {
      const res = next
        ? await fetch("/api/watchlist", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ address }),
          })
        : await fetch(`/api/watchlist?address=${encodeURIComponent(address)}`, { method: "DELETE" });
      // Removing something that is already gone is still the state the user asked for.
      if (!res.ok && !(res.status === 404 && !next)) {
        let msg = `HTTP ${res.status}`;
        try {
          const body = (await res.json()) as { error?: string; detail?: unknown };
          if (typeof body.error === "string") msg = body.error;
          else if (typeof body.detail === "string") msg = body.detail;
        } catch {
          /* keep HTTP status */
        }
        throw new Error(msg);
      }
      startTransition(() => router.refresh());
    } catch (e) {
      setWatched(!next); // roll back
      setError(e instanceof Error ? e.message : "request failed");
    } finally {
      setBusy(false);
    }
  }

  const Icon = busy ? LoaderCircle : watched ? EyeOff : Eye;
  return (
    <div className="flex flex-col items-start gap-2 sm:items-end">
      <button
        type="button"
        onClick={toggle}
        aria-pressed={watched}
        aria-busy={busy}
        disabled={busy}
        className={`inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border px-4 text-sm transition-colors duration-200 disabled:cursor-wait ${
          watched
            ? "border-primary bg-primary/20 text-fg hover:bg-primary/30"
            : "border-border bg-surface text-fg hover:border-fg/40"
        }`}
      >
        <Icon aria-hidden="true" className={`size-4 ${busy ? "motion-safe:animate-spin" : ""}`} />
        {watched ? "Watching" : "Watch"}
      </button>
      <p role="status" aria-live="polite" className={error ? "text-sm text-loss" : "sr-only"}>
        {error ? `Could not update watchlist · ${error}` : ""}
      </p>
    </div>
  );
}
