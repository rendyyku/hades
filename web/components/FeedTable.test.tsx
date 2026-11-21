import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
// Generated from the HL head's own code path (hades_hl.alerts.emit_alerts on a NOTIF follow
// -> alerts.to_contract(...).model_dump(mode="json"), exactly what GET /api/alerts returns
// and what /api/stream sends as `alert` data): open, increase (+50%), reduce (-50%), ladder.
import HL_ALERTS from "@/lib/fixtures/hl-alerts.json";
import { parseFeedEvent, toFeedItems } from "@/lib/live";
import { FeedTable } from "./FeedTable";

const NOW = Date.parse("2026-11-21T12:05:00Z");
const WALLET = "0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1";

function cells(html: string): string[][] {
  return [...html.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/g)]
    .slice(1)
    .map((m) => [...m[1].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map((c) => c[1].replace(/<[^>]+>/g, "").trim()));
}

describe("FeedTable with the HL head's real alert payloads", () => {
  it("reads kind, direction, size, change_pct, coin and the ladder from/to as the HL head emits them", () => {
    const items = toFeedItems("hyperliquid", HL_ALERTS);
    expect(items.map((i) => i.key)).toEqual(["hyperliquid:1", "hyperliquid:2", "hyperliquid:3", "hyperliquid:4"]);
    expect(items.every((i) => i.at === "2026-11-21T12:00:00Z")).toBe(true);
    const html = renderToStaticMarkup(<FeedTable rows={items} now={NOW} caption="t" />);
    const rows = cells(html);
    // [time, head, wallet, event, size, market, ref]
    expect(rows.map((r) => r.slice(3))).toEqual([
      ["OPEN LONG", "2", "BTC", "paper"],
      ["INCREASE LONG", "1 (50%)", "BTC", "paper"],
      ["REDUCE LONG", "1.5 (50%)", "BTC", "paper"],
      ["LADDER", "—", "—", "PAPER → NOTIF"],
    ]);
    expect(html).toContain('dateTime="2026-11-21T12:00:00Z"');
    expect(html).toContain(`href="/arena/hyperliquid/${WALLET}"`);
  });

  it("parses the same payloads off the fan-in (head-tagged SSE data)", () => {
    for (const a of HL_ALERTS) {
      const item = parseFeedEvent(JSON.stringify({ ...a, head: "hyperliquid" }));
      expect(item).toMatchObject({ head: "hyperliquid", key: `hyperliquid:${a.id}`, alert: { kind: a.kind, wallet: WALLET } });
    }
  });
});
