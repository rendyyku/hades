import { describe, expect, it } from "vitest";
import { ART_SIZE, FIST_ANCHORS, HERO_PER_FIST, LINES_PER_THREAD, MAX_THREADS, anchorsNear, generateThreads, type FistAnchor } from "./heroThreads";

/** Angle of a thread in degrees, SVG space (0 = right, 90 = down), normalised into the anchor's fan. */
function angleIn(a: FistAnchor, x2: number, y2: number): number {
  let deg = (Math.atan2(y2 - a.y, x2 - a.x) * 180) / Math.PI;
  while (deg < a.fanFrom - 1e-6) deg += 360;
  while (deg > a.fanTo + 1e-6) deg -= 360;
  return deg;
}

describe("FIST_ANCHORS", () => {
  it("has four anchors inside the art with ordered fans", () => {
    expect(FIST_ANCHORS).toHaveLength(4);
    for (const a of FIST_ANCHORS) {
      expect(a.x).toBeGreaterThan(0);
      expect(a.x).toBeLessThan(ART_SIZE);
      expect(a.y).toBeGreaterThan(0);
      expect(a.y).toBeLessThan(ART_SIZE);
      expect(a.fanTo).toBeGreaterThan(a.fanFrom);
      expect(a.fanTo - a.fanFrom).toBeLessThan(180);
    }
  });
});

describe("generateThreads", () => {
  it("is deterministic for the same seed", () => {
    expect(generateThreads(FIST_ANCHORS, 10, 7)).toEqual(generateThreads(FIST_ANCHORS, 10, 7));
    expect(generateThreads(FIST_ANCHORS, 10, 7)).not.toEqual(generateThreads(FIST_ANCHORS, 10, 8));
  });

  it("emits anchors × perAnchor threads, within the 48-line budget by default", () => {
    expect(generateThreads(FIST_ANCHORS, 10, 7)).toHaveLength(40);
    expect(generateThreads(FIST_ANCHORS.slice(0, 2), 3, 1)).toHaveLength(6);
  });

  it("keeps the hero overlay within the 48-line budget (base + pulse per thread)", () => {
    expect(LINES_PER_THREAD).toBe(2);
    const rendered = generateThreads(FIST_ANCHORS, HERO_PER_FIST, 7).length * LINES_PER_THREAD;
    expect(rendered).toBeLessThanOrEqual(MAX_THREADS);
    expect(rendered).toBe(48);
  });

  it("starts each thread at its anchor and ends beyond the viewBox edge", () => {
    const threads = generateThreads(FIST_ANCHORS, 10, 7);
    threads.forEach((t, i) => {
      const a = FIST_ANCHORS[Math.floor(i / 10)];
      expect([t.x1, t.y1]).toEqual([a.x, a.y]);
      const outside = t.x2 < 0 || t.x2 > ART_SIZE || t.y2 < 0 || t.y2 > ART_SIZE;
      expect(outside).toBe(true);
    });
  });

  it("keeps every angle inside its anchor's fan", () => {
    for (const seed of [1, 7, 42, 1234]) {
      const threads = generateThreads(FIST_ANCHORS, 12, seed);
      threads.forEach((t, i) => {
        const a = FIST_ANCHORS[Math.floor(i / 12)];
        const deg = angleIn(a, t.x2, t.y2);
        expect(deg).toBeGreaterThanOrEqual(a.fanFrom - 1e-6);
        expect(deg).toBeLessThanOrEqual(a.fanTo + 1e-6);
      });
    }
  });

  it("keeps delay in 0..8s and duration in 2.5..5s", () => {
    for (const t of generateThreads(FIST_ANCHORS, 12, 99)) {
      expect(t.delay).toBeGreaterThanOrEqual(0);
      expect(t.delay).toBeLessThanOrEqual(8);
      expect(t.duration).toBeGreaterThanOrEqual(2.5);
      expect(t.duration).toBeLessThanOrEqual(5);
    }
  });

  it("tags each thread with its anchor index", () => {
    const threads = generateThreads(FIST_ANCHORS, 2, 7);
    expect(threads.map((t) => t.anchor)).toEqual([0, 0, 1, 1, 2, 2, 3, 3]);
  });
});

describe("anchorsNear", () => {
  const rect = { left: 100, top: 50, width: 627, height: 627 }; // half-scale render
  it("returns anchors within the radius in screen px", () => {
    const a = FIST_ANCHORS[0];
    const sx = rect.left + a.x / 2;
    const sy = rect.top + a.y / 2;
    expect(anchorsNear(sx + 100, sy, rect, 160)).toEqual([0]);
    expect(anchorsNear(sx + 170, sy, rect, 160)).toEqual([]);
  });
  it("is empty for a zero-size rect", () => {
    expect(anchorsNear(0, 0, { left: 0, top: 0, width: 0, height: 0 }, 160)).toEqual([]);
  });
});
