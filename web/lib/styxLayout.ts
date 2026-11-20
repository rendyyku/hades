// Styx map: pure layout for components/StyxMap (funders → token → early buyers).
import { shortAddr } from "@/lib/format";

export type StyxClass = "loss" | "ember" | "primary" | "muted";

export interface StyxBuyerIn {
  address: string;
  slot: number;
  sol_amount: number | null;
  tags: string[];
  funder?: string | null;
  funder_is_hub?: boolean;
}

export interface StyxInput {
  token: {
    mint: string;
    symbol: string | null;
    deployer: string | null;
    deployer_funder?: string | null;
    pool_created_slot: number | null;
  };
  early_buyers: StyxBuyerIn[];
}

export interface StyxNode {
  id: string;
  kind: "token" | "buyer" | "funder";
  x: number;
  y: number;
  label: string;
  /** Secondary text (buyers: Δslot), rendered muted. */
  sub?: string;
  href?: string;
  cls: StyxClass;
  tags: string[];
  /** Funders: number of buyers funded. */
  count?: number;
  hub?: boolean;
}

export interface StyxLink {
  id: string;
  buyer: string;
  funder: string | null;
  d: string;
  width: number;
  cls: StyxClass;
  dashed: boolean;
}

export interface StyxLayout {
  nodes: StyxNode[];
  links: StyxLink[];
}

export const STYX_MAX_BUYERS = 60;
export const STYX_WIDTH = 1100;
const ROW = 14;
const MIN_HEIGHT = 420;
const PAD_Y = 24;
/** Room for labels outside each column (labels sit left of funders, right of buyers). */
const LABEL_W = 170;
const PAD_X = 16;

/** Default SVG size: 1100 × max(420, 14 × tallest column). */
export function styxSize(buyers: number, funders: number): { width: number; height: number } {
  return { width: STYX_WIDTH, height: Math.max(MIN_HEIGHT, ROW * Math.max(buyers, funders)) };
}

/** Tag priority: deployer_link → loss; bundle → ember dashed; sniper → ember; repeat_early → primary. */
export function styxClass(tags: readonly string[]): { cls: StyxClass; dashed: boolean } {
  if (tags.includes("deployer_link")) return { cls: "loss", dashed: false };
  if (tags.includes("bundle")) return { cls: "ember", dashed: true };
  if (tags.includes("sniper")) return { cls: "ember", dashed: false };
  if (tags.includes("repeat_early")) return { cls: "primary", dashed: false };
  return { cls: "muted", dashed: false };
}

/** Stroke width = 1 + 5·sqrt(sol / max_sol), clamped to 1..6. */
export function styxStroke(sol: number | null | undefined, maxSol: number): number {
  if (sol == null || !(sol > 0) || !(maxSol > 0)) return 1;
  const w = 1 + 5 * Math.sqrt(sol / maxSol);
  return Math.min(6, Math.max(1, w));
}

const r = (n: number) => Math.round(n * 100) / 100;

function spread(i: number, n: number, height: number): number {
  const top = PAD_Y;
  const span = height - 2 * PAD_Y;
  return r(top + ((i + 0.5) * span) / n);
}

/** Cubic segment with horizontal tangents at both ends. */
function seg(x1: number, y1: number, x2: number, y2: number): string {
  const mx = r((x1 + x2) / 2);
  return `C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}

export function computeStyxLayout(input: StyxInput, size: { width: number; height: number }): StyxLayout {
  const { width, height } = size;
  const t = input.token;
  const cx = r(width / 2);
  const cy = r(height / 2);
  const fx = PAD_X + LABEL_W;
  const bx = width - PAD_X - LABEL_W;

  const buyers = [...input.early_buyers]
    .sort((a, b) => a.slot - b.slot || (a.address < b.address ? -1 : a.address > b.address ? 1 : 0))
    .slice(0, STYX_MAX_BUYERS);

  // Unique funders, by funded-buyer count desc, then address.
  const fund = new Map<string, { count: number; hub: boolean }>();
  for (const b of buyers) {
    if (!b.funder) continue;
    const f = fund.get(b.funder) ?? { count: 0, hub: false };
    f.count += 1;
    f.hub = f.hub || !!b.funder_is_hub;
    fund.set(b.funder, f);
  }
  const funderIds = [...fund.keys()].sort((a, b) => fund.get(b)!.count - fund.get(a)!.count || (a < b ? -1 : 1));

  const token: StyxNode = {
    id: t.mint,
    kind: "token",
    x: cx,
    y: cy,
    label: t.symbol || shortAddr(t.mint),
    cls: "primary",
    tags: [],
  };

  const funderNodes: StyxNode[] = funderIds.map((id, i) => {
    const f = fund.get(id)!;
    let label = shortAddr(id);
    let cls: StyxClass = "muted";
    const tags: string[] = [];
    if (t.deployer && id === t.deployer) {
      label = "DEPLOYER";
      cls = "loss";
      tags.push("deployer");
    } else if (t.deployer_funder && id === t.deployer_funder) {
      label = `DEP. FUNDER · ${shortAddr(id)}`;
      cls = "loss";
      tags.push("deployer funder");
    } else if (f.hub) {
      label = `HUB · ${shortAddr(id)}`;
      tags.push("hub");
    }
    if (f.hub && !tags.includes("hub")) tags.push("hub");
    return {
      id,
      kind: "funder",
      x: fx,
      y: spread(i, funderIds.length, height),
      label,
      href: `/wallet/${id}`,
      cls,
      tags,
      count: f.count,
      hub: f.hub,
    };
  });
  const funderPos = new Map(funderNodes.map((n) => [n.id, n]));

  const maxSol = buyers.reduce((m, b) => (b.sol_amount != null && b.sol_amount > m ? b.sol_amount : m), 0);
  const pool = t.pool_created_slot;

  const buyerNodes: StyxNode[] = [];
  const links: StyxLink[] = [];
  buyers.forEach((b, i) => {
    const y = spread(i, buyers.length, height);
    const { cls, dashed } = styxClass(b.tags);
    const delta = pool != null ? b.slot - pool : null;
    buyerNodes.push({
      id: b.address,
      kind: "buyer",
      x: bx,
      y,
      label: shortAddr(b.address),
      sub: delta != null ? `Δ${delta}` : undefined,
      href: `/wallet/${b.address}`,
      cls,
      tags: [...b.tags].sort(),
    });
    const f = b.funder ? funderPos.get(b.funder) : undefined;
    const d = f
      ? `M${f.x},${f.y} ${seg(f.x, f.y, cx, cy)} ${seg(cx, cy, bx, y)}`
      : `M${cx},${cy} ${seg(cx, cy, bx, y)}`;
    links.push({
      id: `${f ? f.id : "∅"}>${b.address}`,
      buyer: b.address,
      funder: f ? f.id : null,
      d,
      width: r(styxStroke(b.sol_amount, maxSol)),
      cls,
      dashed,
    });
  });

  return { nodes: [token, ...buyerNodes, ...funderNodes], links };
}

/**
 * Buyer <-> funder adjacency, built once per layout so highlight checks are
 * O(1) per node instead of scanning every link. Every buyer and funder in the
 * links gets an entry; a buyer without a funder has an empty set.
 */
export function styxNeighbors(links: readonly StyxLink[]): Map<string, Set<string>> {
  const adj = new Map<string, Set<string>>();
  const at = (id: string) => {
    let set = adj.get(id);
    if (!set) adj.set(id, (set = new Set()));
    return set;
  };
  for (const l of links) {
    const b = at(l.buyer);
    if (l.funder) {
      b.add(l.funder);
      at(l.funder).add(l.buyer);
    }
  }
  return adj;
}

/** Highlighted node: the hovered one, else the keyboard-focused one, else none. */
export function styxActive(s: { hover: string | null; focus: string | null }): string | null {
  return s.hover ?? s.focus;
}
