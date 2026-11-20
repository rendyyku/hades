// Pure helpers for the /wallets (Judgement) query string. Safe for server and client.

export const SORTS = ["score", "total_pnl_sol", "win_rate"] as const;
export type WalletSort = (typeof SORTS)[number];

export const TAGS = ["sniper", "bundle", "deployer_link", "repeat_early", "bot"] as const;
export type WalletTagName = (typeof TAGS)[number];

export interface WalletQuery {
  sort: WalletSort;
  tag: string | null;
  hideBots: boolean;
  page: number;
}

export type SearchParams = Record<string, string | string[] | undefined>;

export const DEFAULT_QUERY: WalletQuery = { sort: "score", tag: null, hideBots: true, page: 1 };

function first(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

export function parseWalletQuery(sp: SearchParams): WalletQuery {
  const sortRaw = first(sp.sort);
  const sort = (SORTS as readonly string[]).includes(sortRaw ?? "") ? (sortRaw as WalletSort) : "score";

  const tagRaw = first(sp.tag);
  const tag = tagRaw && (TAGS as readonly string[]).includes(tagRaw) ? tagRaw : null;

  const hb = first(sp.hide_bots)?.trim().toLowerCase();
  const hideBots = !(hb === "0" || hb === "false");

  const n = Number.parseInt(first(sp.page) ?? "", 10);
  const page = Number.isFinite(n) && n >= 1 ? n : 1;

  return { sort, tag, hideBots, page };
}

/** Engine API path for a page of wallets. */
export function walletsApiPath(q: WalletQuery, pageSize = 50): string {
  const p = new URLSearchParams({
    sort: q.sort,
    hide_bots: String(q.hideBots),
    limit: String(pageSize),
    offset: String((q.page - 1) * pageSize),
  });
  if (q.tag) p.set("tag", q.tag);
  return `/api/wallets?${p.toString()}`;
}

/**
 * /wallets URL with `patch` applied. Changing sort, tag or hideBots resets the page to 1.
 * Default values are omitted so links stay short.
 */
export function walletsHref(q: WalletQuery, patch: Partial<WalletQuery>): string {
  const filterChanged =
    ("sort" in patch && patch.sort !== q.sort) ||
    ("tag" in patch && patch.tag !== q.tag) ||
    ("hideBots" in patch && patch.hideBots !== q.hideBots);
  const next: WalletQuery = { ...q, ...patch };
  if (filterChanged) next.page = 1;

  const p = new URLSearchParams();
  if (next.sort !== "score") p.set("sort", next.sort);
  if (next.tag) p.set("tag", next.tag);
  if (!next.hideBots) p.set("hide_bots", "0");
  if (next.page > 1) p.set("page", String(next.page));
  const s = p.toString();
  return s ? `/wallets?${s}` : "/wallets";
}
