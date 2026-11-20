import { describe, expect, it } from "vitest";
import { computeStyxLayout, styxActive, styxNeighbors, styxClass, styxSize, styxStroke, type StyxInput } from "./styxLayout";

const A = (c: string) => c.repeat(32) + "zz";
const MINT = A("M");
const DEP = A("D");
const DEPF = A("E");
const HUB = A("H");
const F1 = A("F");

function buyer(addr: string, slot: number, sol: number | null, tags: string[] = [], funder: string | null = null, hub = false) {
  return { address: addr, slot, sol_amount: sol, tags, funder, funder_is_hub: hub };
}

const INPUT: StyxInput = {
  token: { mint: MINT, symbol: "AAA", deployer: DEP, deployer_funder: DEPF, pool_created_slot: 100 },
  early_buyers: [
    buyer(A("c"), 103, 0.5, ["sniper"], F1),
    buyer(A("b"), 101, 2, ["bundle"], HUB, true),
    buyer(A("a"), 101, 1, ["deployer_link", "sniper"], DEP),
    buyer(A("d"), 105, null, [], null),
    buyer(A("e"), 104, 8, ["repeat_early"], HUB, true),
    buyer(A("f"), 106, 0.1, [], DEPF),
  ],
};
const SIZE = { width: 1100, height: 420 };

describe("computeStyxLayout", () => {
  const L = computeStyxLayout(INPUT, SIZE);
  const buyers = L.nodes.filter((n) => n.kind === "buyer");
  const funders = L.nodes.filter((n) => n.kind === "funder");

  it("counts nodes = 1 + buyers + unique funders", () => {
    expect(L.nodes.filter((n) => n.kind === "token")).toHaveLength(1);
    expect(buyers).toHaveLength(6);
    expect(funders).toHaveLength(4); // F1, HUB, DEP, DEPF
    expect(L.nodes).toHaveLength(1 + 6 + 4);
    expect(L.links).toHaveLength(6);
  });

  it("centers the token, funders left, buyers right", () => {
    const t = L.nodes.find((n) => n.kind === "token")!;
    expect(t.label).toBe("AAA");
    expect(t.x).toBe(550);
    for (const f of funders) expect(f.x).toBeLessThan(t.x);
    for (const b of buyers) expect(b.x).toBeGreaterThan(t.x);
  });

  it("labels the token with shortAddr when it has no symbol", () => {
    const l = computeStyxLayout({ ...INPUT, token: { ...INPUT.token, symbol: null } }, SIZE);
    expect(l.nodes[0].label).toBe("MMMM…MMzz");
  });

  it("orders buyers by slot then address, spread evenly top to bottom", () => {
    expect(buyers.map((b) => b.id)).toEqual([A("a"), A("b"), A("c"), A("e"), A("d"), A("f")]);
    const ys = buyers.map((b) => b.y);
    const gaps = ys.slice(1).map((y, i) => y - ys[i]);
    for (const g of gaps) expect(g).toBeCloseTo(gaps[0], 6);
    expect(gaps[0]).toBeGreaterThan(0);
  });

  it("orders funders by funded buyers desc, then address", () => {
    expect(funders[0].id).toBe(HUB); // funds 2
    expect(funders.map((f) => f.id).slice(1)).toEqual([DEP, DEPF, F1].sort());
  });

  it("labels hubs and the deployer", () => {
    const hub = funders.find((f) => f.id === HUB)!;
    expect(hub.label).toBe("HUB · HHHH…HHzz");
    const dep = funders.find((f) => f.id === DEP)!;
    expect(dep.label).toBe("DEPLOYER");
    expect(dep.cls).toBe("loss");
    expect(funders.find((f) => f.id === F1)!.label).toBe("FFFF…FFzz");
    const depf = funders.find((f) => f.id === DEPF)!;
    expect(depf.label).toBe("DEP. FUNDER · EEEE…EEzz");
    expect(depf.cls).toBe("loss");
  });

  it("links buyer and funder nodes to their wallet page; the token has none", () => {
    for (const n of [...buyers, ...funders]) expect(n.href).toBe(`/wallet/${n.id}`);
    expect(L.nodes[0].href).toBeUndefined();
  });

  it("shows Δslot on buyer nodes", () => {
    expect(buyers[0].sub).toBe("Δ1");
    expect(buyers.find((b) => b.id === A("f"))!.sub).toBe("Δ6");
  });

  it("resolves the color class by tag priority (deployer_link + sniper -> loss)", () => {
    expect(styxClass(["sniper", "deployer_link"])).toEqual({ cls: "loss", dashed: false });
    expect(styxClass(["sniper", "bundle"])).toEqual({ cls: "ember", dashed: true });
    expect(styxClass(["sniper", "repeat_early"])).toEqual({ cls: "ember", dashed: false });
    expect(styxClass(["repeat_early"])).toEqual({ cls: "primary", dashed: false });
    expect(styxClass([])).toEqual({ cls: "muted", dashed: false });
    const link = L.links.find((l) => l.buyer === A("a"))!;
    expect(link.cls).toBe("loss");
    expect(L.links.find((l) => l.buyer === A("b"))!.dashed).toBe(true);
  });

  it("clamps stroke width to 1..6 by sqrt(sol / max)", () => {
    expect(styxStroke(8, 8)).toBe(6);
    expect(styxStroke(2, 8)).toBeCloseTo(3.5, 6);
    expect(styxStroke(null, 8)).toBe(1);
    expect(styxStroke(0, 0)).toBe(1);
    expect(styxStroke(-3, 8)).toBe(1);
    expect(styxStroke(100, 8)).toBe(6);
    const widths = L.links.map((l) => l.width);
    expect(Math.max(...widths)).toBe(6);
    expect(Math.min(...widths)).toBeGreaterThanOrEqual(1);
  });

  it("caps buyers at 60", () => {
    const many = Array.from({ length: 75 }, (_, i) => buyer(`W${String(i).padStart(3, "0")}`.padEnd(36, "x"), 200 + i, 1, [], F1));
    const l = computeStyxLayout({ ...INPUT, early_buyers: many }, SIZE);
    expect(l.nodes.filter((n) => n.kind === "buyer")).toHaveLength(60);
    expect(l.links).toHaveLength(60);
    expect(l.nodes.filter((n) => n.kind === "funder")).toHaveLength(1);
  });

  it("starts a no-funder buyer's path at the center", () => {
    const t = L.nodes[0];
    const link = L.links.find((l) => l.buyer === A("d"))!;
    expect(link.funder).toBeNull();
    expect(link.d.startsWith(`M${t.x},${t.y} `)).toBe(true);
    const withF = L.links.find((l) => l.buyer === A("c"))!;
    const f = funders.find((n) => n.id === F1)!;
    expect(withF.d.startsWith(`M${f.x},${f.y} `)).toBe(true);
    expect(withF.d).toContain(`${t.x},${t.y}`); // passes through the token
  });

  it("is deterministic and independent of input order", () => {
    expect(computeStyxLayout(INPUT, SIZE)).toEqual(L);
    const shuffled = { ...INPUT, early_buyers: [...INPUT.early_buyers].reverse() };
    expect(computeStyxLayout(shuffled, SIZE)).toEqual(L);
  });

  it("handles an empty token", () => {
    const l = computeStyxLayout({ ...INPUT, early_buyers: [] }, SIZE);
    expect(l.nodes).toHaveLength(1);
    expect(l.links).toEqual([]);
  });
});

describe("styxSize", () => {
  it("is 1100 wide and max(420, 14 × tallest column) high", () => {
    expect(styxSize(6, 4)).toEqual({ width: 1100, height: 420 });
    expect(styxSize(60, 10)).toEqual({ width: 1100, height: 840 });
    expect(styxSize(5, 40)).toEqual({ width: 1100, height: 560 });
  });
});

describe("styxNeighbors", () => {
  const L = computeStyxLayout(INPUT, SIZE);
  const adj = styxNeighbors(L.links);

  it("maps each funder to its buyers and each buyer to its funder", () => {
    expect([...adj.get(HUB)!].sort()).toEqual([A("b"), A("e")]);
    expect([...adj.get(A("b"))!]).toEqual([HUB]);
    expect([...adj.get(DEPF)!]).toEqual([A("f")]);
  });

  it("gives a no-funder buyer an empty neighbor set", () => {
    expect(adj.get(A("d"))!.size).toBe(0);
  });
});

describe("styxActive", () => {
  it("prefers the hovered node, then falls back to the focused node, then null", () => {
    expect(styxActive({ hover: "a", focus: "b" })).toBe("a");
    expect(styxActive({ hover: null, focus: "b" })).toBe("b");
    expect(styxActive({ hover: null, focus: null })).toBeNull();
  });
});

