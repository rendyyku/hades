// Token forensics: truncated-history handling (engine Token.history_truncated).

export const TRUNCATION_NOTICE =
  "History truncated: deployer and launch slot unknown; buyers are earliest observed, not launch buyers";

interface AnchorInput {
  pool_created_slot?: number | null;
  history_truncated?: boolean;
}

/** The sniper Δ-slot column needs a real launch anchor. */
export function showDeltaColumn(t: AnchorInput): boolean {
  return !t.history_truncated && t.pool_created_slot != null;
}

/** Extra sentence for the Styx map's accessible label ("" when not truncated). */
export function truncationAriaNote(t: { history_truncated?: boolean }): string {
  return t.history_truncated
    ? "History truncated: buyers are the earliest observed, not the launch buyers; launch slot unknown"
    : "";
}
