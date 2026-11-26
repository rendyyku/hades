import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { SignalRow } from "@/lib/api";
import { SignalTable } from "./SignalTable";

const ROW: SignalRow = {
  id: 7,
  dedupe_key: "k",
  source: "repo_diff",
  kind: "pr_opened",
  severity: "high",
  project_key: "monad:0xabc",
  chain: "monad",
  address: "0xabcdef1234567890",
  symbol: "CAT",
  name: "Cash Cat",
  website: null,
  evidence_url: "https://github.com/trustwallet/assets/pull/1",
  observed_at: "2026-11-27T07:05:00+00:00",
  score: 72,
  score_detail: null,
  listed_on: [],
  enrichment: {
    id: 1,
    signal_id: 7,
    checked_at: "2026-11-27T07:05:00+00:00",
    provider: "geckoterminal",
    buyable: true,
    liquidity_usd: 12500,
    dex: "kuru",
    pool_url: "https://www.geckoterminal.com/monad/pools/0xp",
    volume_24h: null,
    pool_count: 1,
    verified: true,
    candidates: [],
    error: null,
  },
};

describe("SignalTable", () => {
  it("renders the row with a safe evidence link, WIB time and liquidity", () => {
    const html = renderToStaticMarkup(<SignalTable rows={[ROW]} />);
    expect(html).toContain('href="https://github.com/trustwallet/assets/pull/1"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('href="/signal/7"');
    expect(html).toContain("27 Nov 2026 14:05 WIB");
    expect(html).toContain("Bisa dibeli");
    expect(html).toContain("$12.5k");
    expect(html).toContain("repo_diff · pr_opened");
  });

  it("shows an empty state", () => {
    expect(renderToStaticMarkup(<SignalTable rows={[]} />)).toContain("Belum ada sinyal");
  });

  it("never renders a javascript: evidence URL as a link", () => {
    const html = renderToStaticMarkup(<SignalTable rows={[{ ...ROW, evidence_url: "javascript:alert(1)" }]} />);
    expect(html).not.toContain("javascript:");
  });
});
