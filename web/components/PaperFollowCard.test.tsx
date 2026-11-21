import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { PaperDetail } from "@/lib/arena";
import { PaperFollowCard } from "./PaperFollowCard";

const A = "0x" + "aa".repeat(20);
const follow = { origin: "auto", started_at: "2026-11-01T00:00:00Z", ladder: "NOTIF", active: true };

const paper = {
  address: A,
  active: true,
  summary: { address: A, ladder: "NOTIF", equity_usd: "10120.5", net_pnl_usd: "120.5", max_dd: "0.03", closed_trades: 31, days_followed: 29, gap_usd: "-45.25", origin: "auto" },
  gap_usd: "-45.25",
  leader_realized_usd: "900",
  missed_count: 2,
} as unknown as PaperDetail;

describe("PaperFollowCard", () => {
  it("shows the leader-vs-paper gap next to our paper result and links the paper page", () => {
    const html = renderToStaticMarkup(<PaperFollowCard address={A} follow={follow} paper={paper} />);
    expect(html).toContain(`href="/arena/hyperliquid/${A}"`);
    expect(html).toContain("Leader-vs-paper gap");
    expect(html).toContain("−$45.25");
    expect(html).toContain("+$120.50");
    expect(html).toContain("NOTIF");
    expect(html).toContain("31");
  });

  it("keeps the link and says the gap is not known when the paper result is unavailable", () => {
    const html = renderToStaticMarkup(<PaperFollowCard address={A} follow={follow} paper={null} />);
    expect(html).toContain(`href="/arena/hyperliquid/${A}"`);
    expect(html).toContain("Leader-vs-paper gap");
    expect(html).toContain("not available");
  });

  it("says when the follow was stopped", () => {
    const html = renderToStaticMarkup(<PaperFollowCard address={A} follow={{ ...follow, active: false }} paper={paper} />);
    expect(html).toContain("unfollowed");
  });
});
