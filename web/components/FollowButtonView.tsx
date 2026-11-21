import { LoaderCircle, UserCheck, UserPlus } from "lucide-react";
import { UNFOLLOW_WARNING } from "@/lib/followFlow";

type Props = {
  following: boolean;
  busy: boolean;
  /** The unfollow confirm step is open. */
  confirming: boolean;
  error: string | null;
  onToggle: () => void;
  onConfirm: () => void;
  onCancel: () => void;
};

const BTN =
  "inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border px-4 text-sm transition-colors duration-200 disabled:cursor-wait";

/** Presentational part of FollowButton: the toggle, or the unfollow confirm step. */
export function FollowButtonView({ following, busy, confirming, error, onToggle, onConfirm, onCancel }: Props) {
  const Icon = busy ? LoaderCircle : following ? UserCheck : UserPlus;
  return (
    <div className="flex flex-col items-start gap-2 sm:items-end">
      {confirming ? (
        <div
          role="group"
          aria-label="Confirm unfollow"
          className="flex max-w-sm flex-col gap-3 rounded-lg border border-loss/40 bg-loss/10 p-4 text-sm text-fg"
        >
          <p>{UNFOLLOW_WARNING}</p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onConfirm}
              disabled={busy}
              aria-busy={busy}
              className={`${BTN} border-loss/60 bg-surface text-fg hover:border-loss`}
            >
              {busy ? <LoaderCircle aria-hidden="true" className="size-4 motion-safe:animate-spin" /> : null}
              Stop following
            </button>
            <button
              type="button"
              onClick={onCancel}
              disabled={busy}
              className={`${BTN} border-border bg-surface text-fg hover:border-fg/40`}
            >
              Keep following
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={onToggle}
          aria-pressed={following}
          aria-busy={busy}
          disabled={busy}
          className={`${BTN} ${
            following ? "border-hl bg-hl/20 text-fg hover:bg-hl/30" : "border-border bg-surface text-fg hover:border-fg/40"
          }`}
        >
          <Icon aria-hidden="true" className={`size-4 ${busy ? "motion-safe:animate-spin" : ""}`} />
          {following ? "Paper-following" : "Paper-follow"}
        </button>
      )}
      <p role="status" aria-live="polite" className={error ? "text-sm text-loss" : "sr-only"}>
        {error ? `Could not update follow · ${error}` : ""}
      </p>
    </div>
  );
}
