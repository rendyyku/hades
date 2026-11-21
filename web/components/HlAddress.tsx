import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { shortAddr } from "@/lib/format";
import { hlExplorerAddress } from "@/lib/hlView";

const TEXT_LINK =
  "inline-flex cursor-pointer items-center rounded font-mono text-fg underline decoration-border underline-offset-4 transition-colors duration-200 hover:decoration-fg";
const ICON_LINK =
  "inline-flex size-8 shrink-0 cursor-pointer items-center justify-center rounded text-muted transition-colors duration-200 hover:bg-surface hover:text-fg";

/** Hyperliquid address (0x…): internal link plus a Hyperliquid explorer icon link. */
export function HlAddress({ value, full = false, href }: { value: string; full?: boolean; href?: string }) {
  const explorer = hlExplorerAddress(value);
  const text = full ? value : shortAddr(value);
  return (
    <span className="inline-flex max-w-full items-center gap-1">
      {href ? (
        <Link href={href} title={value} className={TEXT_LINK}>
          {text}
        </Link>
      ) : (
        <span title={value} className="break-all font-mono text-fg">
          {text}
        </span>
      )}
      {explorer ? (
        <a
          href={explorer}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Open ${shortAddr(value)} on the Hyperliquid explorer`}
          className={ICON_LINK}
        >
          <ExternalLink aria-hidden="true" className="size-3.5" />
        </a>
      ) : null}
    </span>
  );
}
