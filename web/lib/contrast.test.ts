import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { blend, contrastRatio } from "./contrast";

// Read the real tokens from globals.css so the tests track the design system.
const css = readFileSync(fileURLToPath(new URL("../app/globals.css", import.meta.url)), "utf8");
function token(name: string): string {
  const m = css.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`));
  if (!m) throw new Error(`token --${name} not found as hex in globals.css`);
  return m[1];
}

const BG = token("bg");
const FG = token("fg");
const GAIN = token("gain");
const LOSS = token("loss");
const MUTED = token("muted");
// Row hover in WalletTable: hover:bg-white/[0.03] over --bg.
const HOVER_ALPHA = 0.03;
const HOVER = blend("#ffffff", HOVER_ALPHA, BG);

describe("contrastRatio", () => {
  it("is 21 for black on white and symmetric", () => {
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrastRatio("#ffffff", "#000000")).toBeCloseTo(21, 5);
  });
  it("is 1 for identical colors", () => {
    expect(contrastRatio("#3d3dff", "#3D3DFF")).toBeCloseTo(1, 5);
  });
  it("matches the known --primary on --bg failure (≈2.94)", () => {
    expect(contrastRatio(token("primary"), BG)).toBeCloseTo(2.94, 1);
  });
});

describe("blend", () => {
  it("alpha 0 returns the background, alpha 1 the foreground", () => {
    expect(blend("#ffffff", 0, "#111122")).toBe("#111122");
    expect(blend("#ffffff", 1, "#111122")).toBe("#ffffff");
  });
  it("mixes per channel and rounds", () => {
    expect(blend("#ffffff", 0.5, "#000000")).toBe("#808080");
  });
});

describe("design token contrast (WCAG)", () => {
  it("--fg on --bg ≥ 4.5", () => {
    expect(contrastRatio(FG, BG)).toBeGreaterThanOrEqual(4.5);
  });
  it("--muted on --bg ≥ 4.5", () => {
    expect(contrastRatio(MUTED, BG)).toBeGreaterThanOrEqual(4.5);
  });
  it.each([
    ["gain", GAIN],
    ["loss", LOSS],
  ])("--%s on --bg and on the row hover ≥ 4.5", (_n, c) => {
    expect(contrastRatio(c, BG)).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(c, HOVER)).toBeGreaterThanOrEqual(4.5);
  });
  it("focus ring outer color (--fg) vs --bg ≥ 3", () => {
    expect(css).toMatch(/:focus-visible\s*{[^}]*outline:\s*2px solid var\(--fg\)/);
    expect(contrastRatio(FG, BG)).toBeGreaterThanOrEqual(3);
  });
});
