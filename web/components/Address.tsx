"use client";

import { Check, Copy, ExternalLink } from "lucide-react";
import { useEffect, useState } from "react";
import { shortAddr } from "@/lib/format";

type Props = {
  value: string;
};

/** Solana address: short form in Fira Code, Solscan link and a copy button. */
export function Address({ value }: Props) {
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

  return (
    <span className="inline-flex items-center gap-1 font-mono text-sm">
      <a
        href={`https://solscan.io/account/${value}`}
        target="_blank"
        rel="noopener"
        title={value}
        className="inline-flex cursor-pointer items-center gap-1 rounded text-fg underline decoration-border underline-offset-4 transition-colors duration-200 hover:decoration-fg"
      >
        {shortAddr(value)}
        <ExternalLink aria-hidden="true" className="size-3 text-muted" />
        <span className="sr-only">(opens Solscan in a new tab)</span>
      </a>
      <button
        type="button"
        onClick={copy}
        aria-label="Copy address"
        className="inline-flex size-8 cursor-pointer items-center justify-center rounded text-muted transition-colors duration-200 hover:bg-surface hover:text-fg"
      >
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
