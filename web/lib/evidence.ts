// Evidence links and insider-tag styling. One source for every TagBadge.

export function solscanTx(sig: string): string {
  return `https://solscan.io/tx/${encodeURIComponent(sig)}`;
}

export function solscanAccount(a: string): string {
  return `https://solscan.io/account/${encodeURIComponent(a)}`;
}

export interface TagMeta {
  label: string;
  className: string;
}

// Tailwind theme names only. All text colors are >= 4.5:1 on --bg (primary is
// only used as a border; its text would be 2.94:1).
const BASE = "border";
const STYLES: Record<string, string> = {
  sniper: `${BASE} border-ember text-ember`,
  deployer_link: `${BASE} border-loss text-loss`,
  bundle: `${BASE} border-dashed border-ember text-ember`,
  repeat_early: `${BASE} border-primary text-fg`,
  bot: `${BASE} border-border text-muted`,
};

export function tagMeta(tag: string): TagMeta {
  return {
    label: tag.replace(/_/g, " ").toUpperCase(),
    className: STYLES[tag] ?? STYLES.bot,
  };
}

/** Typed view of the loosely-typed evidence JSON. */
export interface Evidence {
  signatures: string[];
  slots: (number | null)[];
  cluster: string | null;
  funder: string | null;
  chain: string[];
  deltaSlots: number | null;
}

export function readEvidence(ev: Record<string, unknown> | null | undefined): Evidence {
  const e = ev ?? {};
  const strs = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
  const sigs = strs(e.signatures);
  const slotsRaw = Array.isArray(e.slots) ? e.slots : [];
  return {
    signatures: sigs,
    slots: sigs.map((_, i) => (typeof slotsRaw[i] === "number" ? (slotsRaw[i] as number) : null)),
    cluster: typeof e.cluster === "string" ? e.cluster : null,
    funder: typeof e.funder === "string" ? e.funder : null,
    chain: strs(e.chain),
    deltaSlots: typeof e.delta_slots === "number" ? e.delta_slots : null,
  };
}
