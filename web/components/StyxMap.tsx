"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState, type KeyboardEvent } from "react";
import { computeStyxLayout, styxActive, styxNeighbors, styxSize, type StyxClass, type StyxInput, type StyxNode } from "@/lib/styxLayout";
import { ScrollRegion } from "@/components/ScrollRegion";
import { truncationAriaNote } from "@/lib/forensics";

// Literal class names so Tailwind generates them (tokens only).
const STROKE: Record<StyxClass, string> = {
  loss: "stroke-loss",
  ember: "stroke-ember",
  primary: "stroke-primary",
  muted: "stroke-muted",
};
const FILL: Record<StyxClass, string> = {
  loss: "fill-loss",
  ember: "fill-ember",
  primary: "fill-primary",
  muted: "fill-muted",
};

const LEGEND: { cls: StyxClass; label: string; dashed?: boolean }[] = [
  { cls: "loss", label: "Deployer link" },
  { cls: "ember", label: "Bundle", dashed: true },
  { cls: "ember", label: "Sniper" },
  { cls: "primary", label: "Repeat early" },
  { cls: "muted", label: "Untagged" },
];
const SWATCH: Record<StyxClass, string> = {
  loss: "border-loss",
  ember: "border-ember",
  primary: "border-primary",
  muted: "border-muted",
};

const BASE_OPACITY = 0.6;
const DIM = 0.12;

function nodeAria(n: StyxNode): string {
  const kind = n.kind === "funder" ? `Funder${n.count ? ` of ${n.count} buyer${n.count === 1 ? "" : "s"}` : ""}` : "Early buyer";
  const tags = n.tags.length ? `, tags: ${n.tags.map((t) => t.replace(/_/g, " ")).join(", ")}` : "";
  const sub = n.sub ? `, ${n.sub.replace("Δ", "Δ slots ")}` : "";
  return `${kind} ${n.id}${sub}${tags}`;
}

/** Styx map: funders (left) → token (center) → early buyers (right). */
export function StyxMap({ data }: { data: StyxInput }) {
  const router = useRouter();
  // Hover and keyboard focus are tracked separately so leaving a node with the
  // mouse falls back to the focused node instead of clearing it (hover wins).
  const [hover, setHover] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const active = styxActive({ hover, focus });

  const { layout, size, counts } = useMemo(() => {
    const probe = computeStyxLayout(data, styxSize(0, 0));
    const buyers = probe.nodes.filter((n) => n.kind === "buyer").length;
    const funders = probe.nodes.filter((n) => n.kind === "funder");
    const s = styxSize(buyers, funders.length);
    return {
      layout: computeStyxLayout(data, s),
      size: s,
      counts: { buyers, funders: funders.length, hubs: funders.filter((f) => f.hub).length },
    };
  }, [data]);

  const neighbors = useMemo(() => styxNeighbors(layout.links), [layout]);
  const activeNeighbors = active == null ? null : neighbors.get(active);

  const token = layout.nodes[0];
  const go = (href?: string) => {
    if (href) router.push(href);
  };
  const onKey = (e: KeyboardEvent, href?: string) => {
    if (e.key === "Enter") {
      e.preventDefault();
      go(href);
    }
  };

  const summary = `Styx map: ${counts.buyers} early buyer${counts.buyers === 1 ? "" : "s"}, ${counts.funders} funder${
    counts.funders === 1 ? "" : "s"
  }, ${counts.hubs} hub${counts.hubs === 1 ? "" : "s"}${
    data.token.history_truncated ? `. ${truncationAriaNote(data.token)}` : ""
  }`;

  return (
    <div className="styx flex flex-col gap-4">
      <ul className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-muted" aria-label="Styx map legend">
        {LEGEND.map((l) => (
          <li key={l.label} className="inline-flex items-center gap-2">
            <span
              aria-hidden="true"
              className={`inline-block w-6 border-t-2 ${l.dashed ? "border-dashed" : ""} ${SWATCH[l.cls]}`}
            />
            {l.label}
          </li>
        ))}
        <li className="font-mono text-[11px] text-muted">line width = SOL</li>
      </ul>
      <p className="sr-only">The early buyers table above lists the same wallets and is the accessible equivalent of this map.</p>

      <ScrollRegion label="Styx map (scrollable)" className="styx-field rounded-lg border border-border">
        {/* G7: never narrower than the layout width, so viewBox units map >= 1:1
            and the 11px labels never shrink; narrow screens scroll the region. */}
        <div style={{ minWidth: size.width }}>
          <svg
            role="group"
            aria-label={summary}
            viewBox={`0 0 ${size.width} ${size.height}`}
            width="100%"
            className="block h-auto w-full font-mono"
            onMouseLeave={() => setHover(null)}
          >
            <g fill="none">
              {layout.links.map((l) => {
                const on = active == null || l.buyer === active || l.funder === active;
                return (
                  <path
                    key={l.id}
                    d={l.d}
                    strokeWidth={l.width}
                    strokeLinecap="round"
                    strokeDasharray={l.dashed ? "6 5" : undefined}
                    className={`${STROKE[l.cls]} motion-safe:transition-opacity motion-safe:duration-200`}
                    style={{ opacity: active == null ? BASE_OPACITY : on ? 1 : DIM }}
                  />
                );
              })}
            </g>

            {/* Token, center */}
            <g aria-hidden="true">
              <circle cx={token.x} cy={token.y} r={16} className="fill-bg stroke-primary" strokeWidth={2} />
              <circle cx={token.x} cy={token.y} r={5} className="fill-fg" />
              <text x={token.x} y={token.y - 26} textAnchor="middle" className="fill-fg text-[13px] uppercase tracking-[0.12em]">
                {token.label}
              </text>
            </g>

            {layout.nodes.slice(1).map((n) => {
              const left = n.kind === "funder";
              const related =
                active == null || n.id === active || (activeNeighbors?.has(n.id) ?? false);
              return (
                <g
                  key={`${n.kind}:${n.id}`}
                  role="link"
                  tabIndex={0}
                  aria-label={nodeAria(n)}
                  className="styx-node cursor-pointer outline-none motion-safe:transition-opacity motion-safe:duration-200"
                  style={{ opacity: related ? 1 : 0.35 }}
                  onClick={() => go(n.href)}
                  onKeyDown={(e) => onKey(e, n.href)}
                  onMouseEnter={() => setHover(n.id)}
                  onMouseLeave={() => setHover(null)}
                  onFocus={() => setFocus(n.id)}
                  onBlur={() => setFocus(null)}
                >
                  {/* generous hit area across the label */}
                  <rect
                    x={left ? n.x - 168 : n.x - 8}
                    y={n.y - 7}
                    width={176}
                    height={14}
                    className="fill-transparent"
                  />
                  <circle className="styx-ring fill-none stroke-fg" cx={n.x} cy={n.y} r={9} strokeWidth={1.5} />
                  <circle cx={n.x} cy={n.y} r={4} className={FILL[n.cls]} />
                  <text
                    x={left ? n.x - 12 : n.x + 12}
                    y={n.y}
                    dominantBaseline="central"
                    textAnchor={left ? "end" : "start"}
                    className="text-[11px] tabular-nums"
                  >
                    <tspan className="fill-fg">{n.label}</tspan>
                    {n.sub ? <tspan className="fill-muted">{` · ${n.sub}`}</tspan> : null}
                  </text>
                </g>
              );
            })}
          </svg>
        </div>
      </ScrollRegion>
    </div>
  );
}
