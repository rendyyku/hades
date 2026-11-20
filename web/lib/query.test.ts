import { describe, expect, it } from "vitest";
import { parseWalletQuery, walletsApiPath, walletsHref } from "./query";

describe("parseWalletQuery", () => {
  it("applies defaults for an empty query", () => {
    expect(parseWalletQuery({})).toEqual({ sort: "score", tag: null, hideBots: true, page: 1 });
  });
  it("accepts valid values", () => {
    expect(
      parseWalletQuery({ sort: "win_rate", tag: "sniper", hide_bots: "1", page: "3" }),
    ).toEqual({ sort: "win_rate", tag: "sniper", hideBots: true, page: 3 });
    expect(parseWalletQuery({ sort: "total_pnl_sol" }).sort).toBe("total_pnl_sol");
  });
  it("falls back to score for an invalid sort", () => {
    expect(parseWalletQuery({ sort: "address; drop table" }).sort).toBe("score");
    expect(parseWalletQuery({ sort: "" }).sort).toBe("score");
  });
  it("drops unknown tags", () => {
    expect(parseWalletQuery({ tag: "whale" }).tag).toBeNull();
    for (const t of ["sniper", "bundle", "deployer_link", "repeat_early", "bot"]) {
      expect(parseWalletQuery({ tag: t }).tag).toBe(t);
    }
  });
  it("turns hideBots off with hide_bots=0 or false", () => {
    expect(parseWalletQuery({ hide_bots: "0" }).hideBots).toBe(false);
    expect(parseWalletQuery({ hide_bots: "false" }).hideBots).toBe(false);
    expect(parseWalletQuery({ hide_bots: "yes" }).hideBots).toBe(true);
  });
  it("clamps page < 1 and non-numeric pages to 1", () => {
    expect(parseWalletQuery({ page: "0" }).page).toBe(1);
    expect(parseWalletQuery({ page: "-4" }).page).toBe(1);
    expect(parseWalletQuery({ page: "abc" }).page).toBe(1);
    expect(parseWalletQuery({ page: "2.7" }).page).toBe(2);
  });
  it("uses the first value of repeated params", () => {
    expect(parseWalletQuery({ sort: ["win_rate", "score"], page: ["4", "9"] })).toMatchObject({
      sort: "win_rate",
      page: 4,
    });
  });
});

describe("walletsApiPath", () => {
  it("computes the offset from the page", () => {
    expect(walletsApiPath({ sort: "score", tag: null, hideBots: true, page: 1 })).toBe(
      "/api/wallets?sort=score&hide_bots=true&limit=50&offset=0",
    );
    expect(walletsApiPath({ sort: "win_rate", tag: null, hideBots: false, page: 3 })).toBe(
      "/api/wallets?sort=win_rate&hide_bots=false&limit=50&offset=100",
    );
  });
  it("includes the tag when set and honours pageSize", () => {
    expect(walletsApiPath({ sort: "score", tag: "bundle", hideBots: true, page: 2 }, 20)).toBe(
      "/api/wallets?sort=score&hide_bots=true&limit=20&offset=20&tag=bundle",
    );
  });
});

describe("walletsHref", () => {
  const q = { sort: "win_rate", tag: "sniper", hideBots: true, page: 4 } as const;
  it("resets page to 1 when the sort changes", () => {
    expect(walletsHref(q, { sort: "score" })).toBe("/wallets?tag=sniper");
  });
  it("resets page when the tag or hideBots changes", () => {
    expect(walletsHref(q, { tag: null })).toBe("/wallets?sort=win_rate");
    expect(walletsHref(q, { hideBots: false })).toBe("/wallets?sort=win_rate&tag=sniper&hide_bots=0");
  });
  it("keeps the other params when only the page changes", () => {
    expect(walletsHref(q, { page: 5 })).toBe("/wallets?sort=win_rate&tag=sniper&page=5");
  });
  it("returns a bare path for the defaults", () => {
    expect(walletsHref({ sort: "score", tag: null, hideBots: true, page: 1 }, {})).toBe("/wallets");
  });
});
