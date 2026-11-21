import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { readPaperFills, readPaperPositions } from "@/lib/arena";
import { PaperFillsTable } from "./PaperFillsTable";
import { PaperPositionsTable } from "./PaperPositionsTable";

const A = "0x" + "aa".repeat(20);
const HASH = "0x" + "9f".repeat(32);

const fills = readPaperFills([
  { ref: `${HASH}:11`, market_id: "BTC", side: "open", direction: "long", size: "0.05", price: "100000.5", fee: "2.25", realized_pnl: "0", missed: false, position_id: "BTC:1", at: "2026-11-20T10:00:00+00:00" },
  { ref: `${HASH}:12`, market_id: "ETH", side: "open", direction: "short", size: "0", price: null, fee: "0", realized_pnl: "0", missed: true, position_id: "ETH:3", at: "2026-11-20T11:00:00+00:00" },
  { ref: "liquidation:mark:2026-11-20T12:00:00+00:00", market_id: "BTC", side: "close", direction: "long", size: "0.05", price: "90000", fee: "0", realized_pnl: "-500", missed: false, position_id: "BTC:1", at: "2026-11-20T12:00:00+00:00" },
  { ref: `reconcile:${A}:SOL:2:1795000000000`, market_id: "SOL", side: "close", direction: "long", size: "3", price: "150", fee: "0.2", realized_pnl: "12.5", missed: false, position_id: "SOL:2", at: "2026-11-20T13:00:00+00:00" },
]);

describe("PaperFillsTable", () => {
  const html = renderToStaticMarkup(<PaperFillsTable fills={fills} missedCount={1} />);

  it("lists what we copied: coin, side, our price, fee and realized PnL", () => {
    expect(html).toContain("BTC");
    expect(html).toContain("open long");
    expect(html).toContain("$100,000.50");
    expect(html).toContain("$2.25");
    expect(html).toContain("+$12.50");
    expect(html).toContain("−$500.00");
  });

  it("links every leader fill to its transaction", () => {
    expect(html).toContain(`href="https://app.hyperliquid.xyz/explorer/tx/${HASH}"`);
  });

  it("marks a missed event in text and shows no price for it", () => {
    expect(html).toContain("Missed");
    expect(html).toContain("not copied");
  });

  it("labels our liquidation and a reconcile close instead of linking a transaction", () => {
    expect(html).toContain("Paper liquidation");
    expect(html).toContain("Reconcile close");
  });

  it("says so when there is nothing yet", () => {
    expect(renderToStaticMarkup(<PaperFillsTable fills={[]} missedCount={0} />)).toContain("No paper fills yet");
  });
});

describe("PaperPositionsTable", () => {
  it("shows each open paper position with entry, size, margin and leverage", () => {
    const rows = readPaperPositions([
      { position_id: "BTC:4", market_id: "BTC", direction: "short", qty: "0.05", entry_price: "100000", margin: "500", leverage: "10", leader_size: "2.5" },
    ]);
    const html = renderToStaticMarkup(<PaperPositionsTable positions={rows} />);
    expect(html).toContain("BTC");
    expect(html).toContain("short");
    expect(html).toContain("$100,000.00");
    expect(html).toContain("$500.00");
    expect(html).toContain("10x");
    expect(html).toContain("0.05");
  });

  it("says so when no paper position is open", () => {
    expect(renderToStaticMarkup(<PaperPositionsTable positions={[]} />)).toContain("No open paper positions");
  });
});
