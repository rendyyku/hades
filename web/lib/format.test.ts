import { describe, expect, it } from "vitest";
import {
  formatDuration,
  formatPct,
  formatScore,
  formatSol,
  formatUsd,
  formatWinrate,
  relTime,
  shortAddr,
} from "./format";

const MINUS = "\u2212";

describe("formatSol", () => {
  it("prefixes positive values with +", () => {
    expect(formatSol(12.34)).toBe("+12.34 SOL");
  });
  it("uses U+2212 for negatives, not a hyphen", () => {
    const out = formatSol(-3.1);
    expect(out).toBe(`${MINUS}3.10 SOL`);
    expect(out).not.toContain("-");
  });
  it("renders zero without a sign", () => {
    expect(formatSol(0)).toBe("0.00 SOL");
    expect(formatSol(-0)).toBe("0.00 SOL");
  });
  it("uses 4 significant digits below 0.01", () => {
    expect(formatSol(0.001234567)).toBe("+0.001235 SOL");
    expect(formatSol(-0.00005)).toBe(`${MINUS}0.00005000 SOL`);
  });
  it("uses 2 decimals at the 0.01 boundary and groups thousands", () => {
    expect(formatSol(0.01)).toBe("+0.01 SOL");
    expect(formatSol(1234.5)).toBe("+1,234.50 SOL");
  });
  it("returns an em dash for null/undefined/NaN", () => {
    expect(formatSol(null)).toBe("—");
    expect(formatSol(undefined)).toBe("—");
    expect(formatSol(Number.NaN)).toBe("—");
  });
});

describe("formatPct", () => {
  it("formats a ratio with one decimal", () => {
    expect(formatPct(0.634)).toBe("63.4%");
    expect(formatPct(1)).toBe("100.0%");
    expect(formatPct(0)).toBe("0.0%");
  });
  it("returns an em dash for null", () => {
    expect(formatPct(null)).toBe("—");
    expect(formatPct(undefined)).toBe("—");
  });
});

describe("shortAddr", () => {
  it("keeps the first 4 and last 4 characters", () => {
    expect(shortAddr("8DC3abcdefghijklmnopqrstuvwxyzqGxg")).toBe("8DC3…qGxg");
  });
  it("leaves short strings untouched", () => {
    expect(shortAddr("abcdefgh")).toBe("abcdefgh");
  });
  it("returns an em dash for empty input", () => {
    expect(shortAddr("")).toBe("—");
    expect(shortAddr(null)).toBe("—");
  });
});

describe("formatScore", () => {
  it("formats one decimal", () => {
    expect(formatScore(87.456)).toBe("87.5");
    expect(formatScore(0)).toBe("0.0");
  });
  it("returns an em dash for null", () => {
    expect(formatScore(null)).toBe("—");
  });
});

describe("relTime", () => {
  const now = new Date("2026-11-19T12:00:00Z");
  it("formats seconds, minutes, hours, days", () => {
    expect(relTime("2026-11-19T11:59:48Z", now)).toBe("12s ago");
    expect(relTime("2026-11-19T11:55:00Z", now)).toBe("5m ago");
    expect(relTime("2026-11-19T09:00:00Z", now)).toBe("3h ago");
    expect(relTime("2026-11-17T12:00:00Z", now)).toBe("2d ago");
  });
  it("clamps future timestamps to 0s ago", () => {
    expect(relTime("2026-11-19T12:00:05Z", now)).toBe("0s ago");
  });
  it("returns an em dash for null or invalid input", () => {
    expect(relTime(null, now)).toBe("—");
    expect(relTime("not-a-date", now)).toBe("—");
  });
});

describe("formatDuration", () => {
  it("formats seconds, minutes, hours and days", () => {
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(125)).toBe("2m 5s");
    expect(formatDuration(3600)).toBe("1h 0m");
    expect(formatDuration(5430)).toBe("1h 30m");
    expect(formatDuration(90000)).toBe("1d 1h");
  });
  it("rounds fractional seconds", () => {
    expect(formatDuration(44.6)).toBe("45s");
  });
  it("returns an em dash for null, NaN or negative input", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
    expect(formatDuration(-5)).toBe("—");
  });
});

describe("formatWinrate", () => {
  it("renders the winrate with its n", () => {
    expect(formatWinrate(0.64, 42)).toBe("64% (n=42)");
    expect(formatWinrate(0.6449, 42)).toBe("64% (n=42)");
    expect(formatWinrate(1, 3)).toBe("100% (n=3)");
  });
  it("never renders a bare percentage", () => {
    for (const [w, n] of [
      [0.5, null],
      [0.5, undefined],
      [0.5, 0],
      [0.5, -1],
      [0.5, Number.NaN],
      [0.5, 1.5],
    ] as const) {
      expect(formatWinrate(w, n)).not.toMatch(/%/);
    }
  });
  it("shows the dash with n when there is no winrate", () => {
    expect(formatWinrate(null, 0)).toBe("— (n=0)");
    expect(formatWinrate(null, null)).toBe("—");
  });
});

describe("formatUsd", () => {
  it("signs and groups USD", () => {
    expect(formatUsd(1234.5)).toBe("+$1,234.50");
    expect(formatUsd(-3.1)).toBe("\u2212$3.10");
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(null)).toBe("—");
  });
  it("unsigned when asked", () => {
    expect(formatUsd(1234.5, { signed: false })).toBe("$1,234.50");
  });
});
