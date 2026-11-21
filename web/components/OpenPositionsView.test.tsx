import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import fixture from "@/lib/fixtures/hl-positions.json";
import { readPositions, type PositionsFeed } from "@/lib/hlPositions";
import { OpenPositionsView } from "./OpenPositionsView";

const data = readPositions(fixture)!;
const T = 1_795_293_641_000;
const live: PositionsFeed = { data, lastOkAt: T, failed: false };

describe("OpenPositionsView", () => {
  const html = renderToStaticMarkup(<OpenPositionsView feed={live} now={T + 1000} />);

  it("heads the section with the total unrealized PnL and the account value", () => {
    expect(html).toContain("Total unrealized PnL");
    expect(html).toContain("−$1.42");
    expect(html).toContain("Account value");
    expect(html).toContain("$5,437.41");
  });

  it("shows the LIVE badge while polls succeed", () => {
    expect(html).toContain("Live");
    expect(html).not.toContain("Stale");
  });

  it("renders every position of the recorded fixture: side, size, prices, value", () => {
    for (const coin of ["BTC", "ETH", "XRP"]) expect(html).toContain(coin);
    expect(html).toContain("Short");
    expect(html).toContain("Long");
    expect(html).toContain("0.10572");
    expect(html).toContain("$60,387.8"); // entry
    expect(html).toContain("$60,429"); // mark
    expect(html).toContain("$6,388.55"); // position value
    expect(html).toContain("$315.46"); // margin used
  });

  it("colours PnL with the shared gain/loss component, sign and icon included", () => {
    expect(html).toMatch(/text-loss[^>]*>[\s\S]*?−\$4\.35/);
    expect(html).toMatch(/text-gain[^>]*>[\s\S]*?\+\$2\.94/);
    expect(html).toContain("−1.36%");
    expect(html).toContain("+2.33%");
  });

  it("shows leverage with its margin mode, and a dash for no liquidation price", () => {
    expect(html).toContain("20x isolated");
    expect(html).toContain("20x cross");
    expect(html).toContain("$62,630.1");
    expect(html).toMatch(/Liq\. price<\/dt><dd[^>]*>—/);
  });

  it("renders a table for wide screens and cards for mobile", () => {
    expect(html).toContain("<table");
    expect(html).toContain("hidden sm:block");
    expect(html).toContain("sm:hidden");
    expect(html.match(/<tbody>[\s\S]*<\/tbody>/)![0].match(/<tr/g)).toHaveLength(3);
  });

  it("says which dex the list covers", () => {
    expect(html).toContain("main perp dex");
    expect(html).toContain("HIP-3");
  });

  it("stale: a failed poll keeps the last data and says so in text", () => {
    const stale = renderToStaticMarkup(
      <OpenPositionsView feed={{ data, lastOkAt: T, failed: true }} now={T + 12_000} />,
    );
    expect(stale).toContain("Stale · 12s ago");
    expect(stale).not.toContain(">Live<");
    expect(stale).toContain("Could not refresh");
    expect(stale).toContain("BTC"); // the last known positions stay on screen
  });

  it("offline: never loaded shows no numbers, only the reason", () => {
    const off = renderToStaticMarkup(<OpenPositionsView feed={{ data: null, lastOkAt: null, failed: true }} now={T} />);
    expect(off).toContain("Stale · never");
    expect(off).toContain("Open positions are not available");
    expect(off).not.toContain("<table");
    expect(off).not.toContain("$");
  });

  it("a flat wallet says so", () => {
    const flat = renderToStaticMarkup(
      <OpenPositionsView feed={{ data: { ...data, positions: [], totalUnrealizedPnl: 0 }, lastOkAt: T, failed: false }} now={T} />,
    );
    expect(flat).toContain("No open positions");
    expect(flat).toContain("Account value");
  });
});
