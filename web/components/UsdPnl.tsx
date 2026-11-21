import { Minus, TrendingDown, TrendingUp } from "lucide-react";
import { formatUsd } from "@/lib/format";

/** Signed USD amount with a trend icon and gain/loss color (never color alone). */
export function UsdPnl({ value, align = "end" }: { value: number | null; align?: "start" | "end" }) {
  const justify = align === "end" ? "justify-end" : "justify-start";
  if (value === null || value === 0 || !Number.isFinite(value)) {
    return (
      <span className={`inline-flex items-center ${justify} gap-1.5 text-muted`}>
        <Minus aria-hidden="true" className="size-3.5" />
        {formatUsd(value)}
      </span>
    );
  }
  const up = value > 0;
  const Icon = up ? TrendingUp : TrendingDown;
  return (
    <span className={`inline-flex items-center ${justify} gap-1.5 ${up ? "text-gain" : "text-loss"}`}>
      <Icon aria-hidden="true" className="size-3.5" />
      {formatUsd(value)}
    </span>
  );
}
