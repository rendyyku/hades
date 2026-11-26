import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Nav, NAV_LINKS } from "./Nav";

describe("Nav", () => {
  it("lists the HADES v2 pages in spec 9b order", () => {
    expect(NAV_LINKS.map((l) => l.label)).toEqual(["Overview", "Signals", "Listings", "Watchlist", "Eval"]);
  });

  it("links every page and drops the v1 heads", () => {
    const html = renderToStaticMarkup(<Nav />);
    for (const href of ["/", "/signals", "/listings", "/watchlist", "/eval"]) {
      expect(html).toContain(`href="${href}"`);
    }
    expect(html).not.toContain("Hyperliquid");
    expect(html).not.toContain("Polymarket");
  });
});
