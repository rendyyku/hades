import type { ReactNode } from "react";

type Props = {
  index: string;
  title: string;
  action?: ReactNode;
  children: ReactNode;
};

/** Hermes-style numbered section: small `#1` index, big thin Oswald title, 1px rule. */
export function Section({ index, title, action, children }: Props) {
  const id = `section-${index.replace(/[^0-9a-z]/gi, "")}`;
  return (
    <section aria-labelledby={id} className="border-t border-border pt-8 sm:pt-10">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4 sm:mb-8">
        <h2 id={id} className="flex items-baseline gap-4">
          <span className="font-mono text-sm tabular-nums text-ember">{index}</span>
          <span className="font-display text-3xl font-light uppercase leading-none tracking-tight sm:text-5xl">
            {title}
          </span>
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}
