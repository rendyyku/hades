import { describe, expect, it } from "vitest";
import { solscanAccount, solscanTx, tagMeta } from "./evidence";

describe("solscan links", () => {
  it("builds tx and account URLs", () => {
    expect(solscanTx("5abc")).toBe("https://solscan.io/tx/5abc");
    expect(solscanAccount("9xyz")).toBe("https://solscan.io/account/9xyz");
  });
  it("encodes unsafe characters", () => {
    expect(solscanTx("a/b?c")).toBe("https://solscan.io/tx/a%2Fb%3Fc");
  });
});

describe("tagMeta", () => {
  it("maps sniper to ember", () => {
    const m = tagMeta("sniper");
    expect(m.label).toBe("SNIPER");
    expect(m.className).toContain("text-ember");
    expect(m.className).toContain("border-ember");
    expect(m.className).not.toContain("border-dashed");
  });
  it("maps deployer_link to loss", () => {
    const m = tagMeta("deployer_link");
    expect(m.label).toBe("DEPLOYER LINK");
    expect(m.className).toContain("text-loss");
    expect(m.className).toContain("border-loss");
  });
  it("maps bundle to a dashed ember outline", () => {
    const m = tagMeta("bundle");
    expect(m.label).toBe("BUNDLE");
    expect(m.className).toContain("text-ember");
    expect(m.className).toContain("border-dashed");
    expect(m.className).toContain("border-ember");
  });
  it("maps repeat_early to fg text on a primary border", () => {
    const m = tagMeta("repeat_early");
    expect(m.label).toBe("REPEAT EARLY");
    expect(m.className).toContain("text-fg");
    expect(m.className).toContain("border-primary");
  });
  it("maps bot to muted", () => {
    const m = tagMeta("bot");
    expect(m.label).toBe("BOT");
    expect(m.className).toContain("text-muted");
    expect(m.className).toContain("border-border");
  });
  it("falls back to the bot style for unknown tags", () => {
    const m = tagMeta("whale_watcher");
    expect(m.label).toBe("WHALE WATCHER");
    expect(m.className).toBe(tagMeta("bot").className);
  });
});
