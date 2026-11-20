import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: () => {} }) }));

import { StyxMap } from "./StyxMap";

const A = (c: string) => c.repeat(32) + "zz";

describe("StyxMap", () => {
  const html = renderToStaticMarkup(
    <StyxMap
      data={{
        token: { mint: A("M"), symbol: "AAA", deployer: A("D"), deployer_funder: null, pool_created_slot: 100 },
        early_buyers: [{ address: A("b"), slot: 101, sol_amount: 1, tags: [], funder: A("F"), funder_is_hub: false }],
      }}
    />,
  );

  it("keeps the drawing at its layout width so 11px labels never shrink (G7)", () => {
    expect(html).toMatch(/style="min-width:1100px"/);
  });

  it("scrolls inside a relative ScrollRegion (G6)", () => {
    const region = html.match(/<div[^>]*role="region"[^>]*>/)![0];
    expect(region).toMatch(/class="[^"]*\brelative\b[^"]*"/);
    expect(region).toContain("overflow-x-auto");
    expect(region).toContain('tabindex="0"');
  });
});
