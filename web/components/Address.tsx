"use client";

import { Check, Copy, ExternalLink } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { solscanAccount } from "@/lib/evidence";
import { shortAddr } from "@/lib/format";

type Props = {
  value: string;
  /** Show the full address instead of the 4…4 short form. */
  full?: boolean;
  /** Internal link (e.g. /wallet/<addr>); Solscan then moves to a separate icon link. */
  href?: string;
};

const TEXT_LINK =
  "inline-flex cursor-pointer items-center gap-1 rounded text-fg underline decoration-border underline-offset-4 transition-colors duration-200 hover:decoration-fg";
const ICON_BTN =
  "inline-flex size-8 shrink-0 cursor-pointer items-center justify-center rounded text-muted transition-colors duration-200 hover:bg-surface hover:text-fg";

/** Solana address in Fira Code with a Solscan link and a copy button. */
export function Address({ value, full = false, href }: Props) {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");
  const copied = status === "copied";

  useEffect(() => {
    if (status === "idle") return;
    const t = setTimeout(() => setStatus("idle"), 1500);
    return () => clearTimeout(t);
  }, [status]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setStatus("copied");
    } catch {
      setStatus("failed");
    }
  }

  const text = full ? value : shortAddr(value);
  const textCls = full ? "min-w-0 break-all" : "";

  return (
    <span className="inline-flex max-w-full items-center gap-1 font-mono text-sm">
      {href ? (
        <>
          <Link href={href} title={value} className={`${TEXT_LINK} ${textCls}`}>
            {text}
          </Link>
          <a
            href={solscanAccount(value)}
            target="_blank"
            rel="noopener"
            aria-label={`View ${shortAddr(value)} on Solscan (opens in a new tab)`}
            className={ICON_BTN}
          >
            <ExternalLink aria-hidden="true" className="size-3.5" />
          </a>
        </>
      ) : (
        <a
          href={solscanAccount(value)}
          target="_blank"
          rel="noopener"
          title={value}
          className={`${TEXT_LINK} ${textCls}`}
        >
          {text}
          <ExternalLink aria-hidden="true" className="size-3 shrink-0 text-muted" />
          <span className="sr-only">(opens Solscan in a new tab)</span>
        </a>
      )}
      <button type="button" onClick={copy} aria-label={`Copy address ${shortAddr(value)}`} className={ICON_BTN}>
        {copied ? (
          <Check aria-hidden="true" className="size-3.5 text-gain" />
        ) : (
          <Copy aria-hidden="true" className="size-3.5" />
        )}
      </button>
      <span role="status" aria-live="polite" className="sr-only">
        {status === "copied" ? "Address copied" : status === "failed" ? "Copy failed" : ""}
      </span>
    </span>
  );
}
