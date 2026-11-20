import { describe, expect, it } from "vitest";
import { ALL_TIME_LABEL, scoreReason, statsWindowLabel } from "./walletView";

const stats = (over: Record<string, unknown> = {}) => ({ score: null, tokens_closed: 2, ...over });

describe("statsWindowLabel", () => {
  it("labels the stats window from the payload", () => {
    expect(statsWindowLabel(30)).toBe("Last 30d");
    expect(statsWindowLabel(7)).toBe("Last 7d");
    expect(ALL_TIME_LABEL).toBe("All time");
  });
});

describe("scoreReason", () => {
  it("is null when the wallet has a score", () => {
    expect(scoreReason({ stats: stats({ score: 81 }), is_bot: false, min_tokens_closed: 5, window_days: 30 })).toBeNull();
  });

  it("explains a bot", () => {
    expect(scoreReason({ stats: stats(), is_bot: true, min_tokens_closed: 5, window_days: 30 })).toMatch(/bot/i);
  });

  it("explains too few closed tokens", () => {
    expect(scoreReason({ stats: stats({ tokens_closed: 2 }), is_bot: false, min_tokens_closed: 5, window_days: 30 })).toBe(
      "No score: 2 closed tokens in the last 30d; needs at least 5",
    );
    expect(scoreReason({ stats: stats({ tokens_closed: 1 }), is_bot: false, min_tokens_closed: 5, window_days: 30 })).toBe(
      "No score: 1 closed token in the last 30d; needs at least 5",
    );
  });

  it("covers wallets without stats", () => {
    expect(scoreReason({ stats: null, is_bot: false, min_tokens_closed: 5, window_days: 30 })).toBe(
      "Not scored yet (needs at least 5 closed tokens)",
    );
  });
});
