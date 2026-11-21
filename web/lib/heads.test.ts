import { describe, expect, it } from "vitest";
import { getHead, HEADS, isHeadId } from "./heads";

describe("head registry", () => {
  it("has unique ids", () => {
    const ids = HEADS.map((h) => h.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("has unique env names and accents per head", () => {
    expect(new Set(HEADS.map((h) => h.baseUrlEnv)).size).toBe(HEADS.length);
    expect(new Set(HEADS.map((h) => h.keyEnv)).size).toBe(HEADS.length);
    expect(new Set(HEADS.map((h) => h.accent)).size).toBe(HEADS.length);
  });

  it("registers solana (engine :8000, ember) and hyperliquid (:8010, hl)", () => {
    expect(getHead("solana")).toMatchObject({
      label: "Solana",
      baseUrlEnv: "HADES_API_URL",
      keyEnv: "HADES_API_KEY",
      defaultUrl: "http://127.0.0.1:8000",
      accent: "ember",
    });
    expect(getHead("hyperliquid")).toMatchObject({
      label: "Hyperliquid",
      baseUrlEnv: "HL_API_URL",
      keyEnv: "HL_API_KEY",
      defaultUrl: "http://127.0.0.1:8010",
      accent: "hl",
    });
  });

  it("binds every default URL to 127.0.0.1", () => {
    for (const h of HEADS) expect(new URL(h.defaultUrl).hostname).toBe("127.0.0.1");
  });

  it("validates head ids", () => {
    expect(isHeadId("hyperliquid")).toBe(true);
    expect(isHeadId("polymarket")).toBe(false);
    expect(isHeadId("__proto__")).toBe(false);
  });

  it("knows each head's address format", () => {
    const hl = getHead("hyperliquid");
    expect(hl.address.test("0x" + "ab".repeat(20))).toBe(true);
    expect(hl.address.test("0x" + "ab".repeat(19))).toBe(false);
    expect(hl.address.test("0x" + "zz".repeat(20))).toBe(false);
    expect(getHead("solana").address.test("So11111111111111111111111111111111111111112")).toBe(true);
  });
});
