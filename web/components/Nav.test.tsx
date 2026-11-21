import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Nav, NAV_LINKS } from "./Nav";

describe("Nav", () => {
  it("follows the spec order, with Solana instead of Judgement", () => {
    expect(NAV_LINKS.map((l) => l.label)).toEqual([
      "Overview",
      "Solana",
      "Hyperliquid",
      "Polymarket",
      "Paper Arena",
      "Cerberus",
      "Watchlist",
    ]);
  });

  it("renders Polymarket disabled, without a link, and links the Paper Arena", () => {
    const html = renderToStaticMarkup(<Nav />);
    expect(html).not.toContain("Judgement");
    expect(html).toMatch(/<span aria-disabled="true"[^>]*>Polymarket/);
    expect(html).not.toContain('href="/polymarket"');
    expect(html).toContain('href="/arena"');
  });
});
