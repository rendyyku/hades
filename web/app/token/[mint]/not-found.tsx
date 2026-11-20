import { ArrowLeft } from "lucide-react";
import Link from "next/link";

export default function TokenNotFound() {
  return (
    <div className="flex flex-col gap-6 pt-14 sm:pt-20">
      <p className="font-mono text-xs uppercase tracking-[0.2em] text-muted">404</p>
      <h1 className="font-display text-[clamp(40px,7vw,88px)] font-light uppercase leading-[0.9] tracking-tight text-fg">
        Token not found
      </h1>
      <p className="max-w-xl text-muted">This mint is not valid or HADES has not indexed it yet.</p>
      <Link
        href="/"
        className="inline-flex min-h-11 w-fit cursor-pointer items-center gap-2 rounded text-sm text-fg underline decoration-primary decoration-2 underline-offset-8"
      >
        <ArrowLeft aria-hidden="true" className="size-4" /> Back to Overview
      </Link>
    </div>
  );
}
