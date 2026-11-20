import type { ReactNode } from "react";

/**
 * Horizontally scrollable, keyboard-focusable region for wide tables / maps.
 *
 * `relative` is required: descendants such as `sr-only` text are
 * `position: absolute`, and without a positioned ancestor here they resolve
 * against an outer containing block, escape this overflow box and widen the
 * whole page (horizontal page scroll at 375px). `relative` keeps them clipped
 * inside the scroller. Server-safe: no hooks, no "use client".
 */
export function ScrollRegion({
  label,
  className,
  children,
}: {
  label: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div role="region" aria-label={label} tabIndex={0} className={"relative overflow-x-auto " + (className ?? "")}>
      {children}
    </div>
  );
}
