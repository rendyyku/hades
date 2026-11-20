"use client";

import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import {
  ART_SIZE,
  FIST_ANCHORS,
  HERO_PER_FIST,
  LINES_PER_THREAD,
  MAX_THREADS,
  anchorsNear,
  generateThreads,
} from "@/lib/heroThreads";

const HOVER_RADIUS = 160;

const REDUCED = "(prefers-reduced-motion: reduce)";
function subscribeReduced(cb: () => void) {
  const mq = window.matchMedia(REDUCED);
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
}
const getReduced = () => window.matchMedia(REDUCED).matches;
const getReducedServer = () => false;

/**
 * Animated SVG threads over the hero art, in the art's pixel space. Motion is
 * CSS-only (keyframes hades-thread-pulse in globals.css); JS only toggles
 * classes: pause offscreen / hidden tab, and brighten the fist near the pointer.
 */
export function ThreadsOverlay() {
  const ref = useRef<SVGSVGElement>(null);
  // Each thread draws base + pulse; the slice enforces the 48-line budget.
  const threads = useMemo(() => generateThreads(FIST_ANCHORS, HERO_PER_FIST, 7).slice(0, MAX_THREADS / LINES_PER_THREAD), []);
  const reduced = useSyncExternalStore(subscribeReduced, getReduced, getReducedServer);
  const [active, setActive] = useState<string>("");
  const [offscreen, setOffscreen] = useState(false);
  const [hidden, setHidden] = useState(false);

  // Pause offscreen (IntersectionObserver) and in background tabs.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(([e]) => setOffscreen(!e.isIntersecting), { threshold: 0 });
    io.observe(el);
    const onVis = () => setHidden(document.hidden);
    onVis();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      io.disconnect();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  // One pointermove listener, rAF-throttled; state only changes when the set does.
  useEffect(() => {
    let frame = 0;
    let px = 0;
    let py = 0;
    const tick = () => {
      frame = 0;
      const el = ref.current;
      if (!el) return;
      const next = anchorsNear(px, py, el.getBoundingClientRect(), HOVER_RADIUS).join(",");
      setActive((prev) => (prev === next ? prev : next));
    };
    const onMove = (e: PointerEvent) => {
      px = e.clientX;
      py = e.clientY;
      if (!frame) frame = requestAnimationFrame(tick);
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (frame) cancelAnimationFrame(frame);
    };
  }, []);

  const activeSet = new Set(active ? active.split(",").map(Number) : []);
  const paused = offscreen || hidden;

  return (
    <svg
      ref={ref}
      viewBox={`0 0 ${ART_SIZE} ${ART_SIZE}`}
      aria-hidden="true"
      focusable="false"
      preserveAspectRatio="xMidYMid slice"
      className={`hades-threads pointer-events-none absolute inset-0 size-full overflow-visible${paused ? " is-paused" : ""}`}
    >
      {FIST_ANCHORS.map((_, ai) => (
        <g key={ai} data-anchor={ai} className={`hades-fist${activeSet.has(ai) ? " is-near" : ""}`}>
          {threads
            .filter((t) => t.anchor === ai)
            .map((t, i) => (
              <g key={i}>
                <line className="hades-thread-base" x1={t.x1} y1={t.y1} x2={t.x2} y2={t.y2} />
                {reduced ? null : (
                  <line
                    className="hades-pulse"
                    x1={t.x1}
                    y1={t.y1}
                    x2={t.x2}
                    y2={t.y2}
                    pathLength={100}
                    style={{ animationDuration: `${t.duration}s`, animationDelay: `${t.delay}s` }}
                  />
                )}
              </g>
            ))}
        </g>
      ))}
    </svg>
  );
}
