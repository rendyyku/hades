// Hero "many threads" overlay: pure geometry for components/ThreadsOverlay.
// Coordinates are in the art's own pixel space (1254 × 1254, the SVG viewBox).
// Angles are degrees in SVG space: 0 = right, 90 = down, 180 = left, 270 = up.

export const ART_SIZE = 1254;
/** Hard budget for rendered base lines (performance, Task 12b G4). */
export const MAX_THREADS = 48;

export interface FistAnchor {
  x: number;
  y: number;
  /** Fan start/end angle (degrees, SVG space). fanFrom < fanTo; may exceed 360 or go negative. */
  fanFrom: number;
  fanTo: number;
}

export interface Thread {
  anchor: number;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  /** seconds, 0..8 */
  delay: number;
  /** seconds, 2.5..5 */
  duration: number;
}

/**
 * The four fists, checked against the 1254² art: each point sits where the
 * engraved threads converge in the knuckles, and each fan follows the engraved
 * thread bundle leaving that fist.
 */
export const FIST_ANCHORS: readonly FistAnchor[] = [
  // top-left fist (raised), threads leave up-left toward the left/top edges
  { x: 215, y: 212, fanFrom: 188, fanTo: 248 },
  // top-right fist (raised), threads leave up-right toward the right/top edges
  { x: 1030, y: 272, fanFrom: 292, fanTo: 352 },
  // mid-left fist (lower arm), threads leave left and down-left
  { x: 185, y: 772, fanFrom: 150, fanTo: 205 },
  // mid-right fist (lower arm), threads leave right and down-right
  { x: 1095, y: 808, fanFrom: -10, fanTo: 48 },
];

/** mulberry32: tiny deterministic PRNG, returns floats in [0, 1). */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Distance along (dx, dy) from (x, y) to the edge of [0, size]². */
function distToEdge(x: number, y: number, dx: number, dy: number, size: number): number {
  const tx = dx > 1e-9 ? (size - x) / dx : dx < -1e-9 ? -x / dx : Infinity;
  const ty = dy > 1e-9 ? (size - y) / dy : dy < -1e-9 ? -y / dy : Infinity;
  return Math.min(tx, ty);
}

const OVERSHOOT = 60; // px past the viewBox edge, so the end is never visible

const r2 = (n: number) => Math.round(n * 100) / 100;

/**
 * Deterministic threads: `perAnchor` per fist, each from the fist to a point
 * beyond the viewBox edge at an angle inside that fist's fan. Angles are
 * jittered within evenly spaced slots so a fan never clumps.
 */
export function generateThreads(anchors: readonly FistAnchor[], perAnchor = 10, seed = 7): Thread[] {
  const rnd = mulberry32(seed);
  const out: Thread[] = [];
  anchors.forEach((a, ai) => {
    const span = a.fanTo - a.fanFrom;
    for (let i = 0; i < perAnchor; i++) {
      const slot = span / perAnchor;
      const deg = a.fanFrom + slot * (i + 0.15 + 0.7 * rnd());
      const rad = (deg * Math.PI) / 180;
      const dx = Math.cos(rad);
      const dy = Math.sin(rad);
      const len = distToEdge(a.x, a.y, dx, dy, ART_SIZE) + OVERSHOOT;
      out.push({
        anchor: ai,
        x1: a.x,
        y1: a.y,
        x2: r2(a.x + dx * len),
        y2: r2(a.y + dy * len),
        delay: r2(rnd() * 8),
        duration: r2(2.5 + rnd() * 2.5),
      });
    }
  });
  return out;
}

export interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Anchor indices within `radius` screen px of the pointer, given the overlay's client rect. */
export function anchorsNear(
  px: number,
  py: number,
  rect: Rect,
  radius = 160,
  anchors: readonly FistAnchor[] = FIST_ANCHORS,
): number[] {
  if (rect.width <= 0 || rect.height <= 0) return [];
  const sx = rect.width / ART_SIZE;
  const sy = rect.height / ART_SIZE;
  const out: number[] = [];
  anchors.forEach((a, i) => {
    const ax = rect.left + a.x * sx;
    const ay = rect.top + a.y * sy;
    if (Math.hypot(px - ax, py - ay) <= radius) out.push(i);
  });
  return out;
}
