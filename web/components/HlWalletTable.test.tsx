import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { HlWalletRow } from "@/lib/hlView";
import { HlWalletTable } from "./HlWalletTable";

const A = "0x" + "ab".repeat(20);
const B = "0x" + "cd".repeat(20);

const rows: HlWalletRow[] = [
  { address: A, score: 81.2, net_pnl_usd: "1234.5", trades: 300, winrate: 0.64, n: 42, tags: ["pre_move"], is_bot: false, extra: {} },
  { address: B, score: null, net_pnl_usd: "0", trades: 3, winrate: null, n: 0, tags: [], is_bot: false, extra: {} },
];

describe("HlWalletTable", () => {
  const html = renderToStaticMarkup(<HlWalletTable rows={rows} />);

  it("renders the winrate with its n", () => {
    expect(html).toContain("64% (n=42)");
  });

  it("never renders a bare winrate percentage", () => {
    const pcts = html.match(/\d+%(?! \(n=\d+\))/g) ?? [];
    expect(pcts).toEqual([]);
    expect(html).toContain("— (n=0)");
  });

  it("links wallets to the Hyperliquid wallet page", () => {
    expect(html).toContain(`href="/hyperliquid/wallet/${A}"`);
  });

  it("formats net PnL in USD", () => {
    expect(html).toContain("+$1,234.50");
  });

  it("has an empty state", () => {
    expect(renderToStaticMarkup(<HlWalletTable rows={[]} />)).toContain("No scored Hyperliquid wallets yet");
  });
});
