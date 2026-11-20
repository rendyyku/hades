// Labels for the wallet page: stats window vs all-time sections, and why a wallet has no score.

export const ALL_TIME_LABEL = "All time";

/** Stats cards cover the engine's rolling window (WalletDetail.window_days). */
export function statsWindowLabel(days: number): string {
  return `Last ${days}d`;
}

interface ScoreInput {
  stats: { score: number | null; tokens_closed: number | null } | null;
  is_bot: boolean;
  min_tokens_closed: number;
  window_days: number;
}

/** Why the score is empty (null when there is a score). */
export function scoreReason(d: ScoreInput): string | null {
  const min = d.min_tokens_closed;
  if (!d.stats) return `Not scored yet (needs at least ${min} closed tokens)`;
  if (d.stats.score != null) return null;
  if (d.is_bot) return "No score: flagged as a bot (over 150 trades/day or a median hold under 30 s)";
  const closed = d.stats.tokens_closed ?? 0;
  if (closed < min) {
    return `No score: ${closed} closed token${closed === 1 ? "" : "s"} in the last ${d.window_days}d; needs at least ${min}`;
  }
  return "No score";
}
