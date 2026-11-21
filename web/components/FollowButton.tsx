"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { sendFollow, toggleIntent, type FollowAction } from "@/lib/followFlow";
import type { HeadId } from "@/lib/heads";
import { FollowButtonView } from "./FollowButtonView";

type Props = {
  head: HeadId;
  address: string;
  initialFollowing: boolean;
};

/**
 * Paper-follow toggle. Talks to the same-origin, origin-guarded proxy
 * (/api/heads/<head>/follows); the head key stays on the server. Unfollowing needs a
 * second, explicit step: it discards the forward test (a re-follow starts a new one).
 */
export function FollowButton({ head, address, initialFollowing }: Props) {
  const router = useRouter();
  const [following, setFollowing] = useState(initialFollowing);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [, startTransition] = useTransition();

  async function run(action: FollowAction) {
    if (busy) return;
    const next = action === "follow";
    setFollowing(next); // optimistic
    setError(null);
    setBusy(true);
    const res = await sendFollow(head, address, action);
    if (res.ok) {
      startTransition(() => router.refresh());
    } else {
      setFollowing(!next); // roll back
      setError(res.error);
    }
    setConfirming(false);
    setBusy(false);
  }

  function toggle() {
    if (busy) return;
    if (toggleIntent(following) === "confirm-unfollow") {
      setError(null);
      setConfirming(true);
    } else {
      void run("follow");
    }
  }

  return (
    <FollowButtonView
      following={following}
      busy={busy}
      confirming={confirming}
      error={error}
      onToggle={toggle}
      onConfirm={() => void run("unfollow")}
      onCancel={() => setConfirming(false)}
    />
  );
}
