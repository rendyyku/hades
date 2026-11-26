import { CircleCheck, TriangleAlert } from "lucide-react";
import type { WatcherRow } from "@/lib/api";
import { relTime } from "@/lib/format";
import { wib } from "@/lib/signalView";

/** One card per watcher: OK (last success) or failing since … with the last error. */
export function WatcherHealth({ watchers }: { watchers: WatcherRow[] }) {
  if (!watchers.length) {
    return <p className="rounded-lg border border-border bg-surface p-6 text-muted">Belum ada watcher yang jalan.</p>;
  }
  return (
    <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {watchers.map((w) => {
        const failing = w.fail_count > 0;
        return (
          <li
            key={w.name}
            className={`rounded-lg border p-4 backdrop-blur-md ${failing ? "border-loss/40 bg-loss/10" : "border-border bg-surface"}`}
          >
            <p className="flex items-center gap-2 break-all font-mono text-xs text-fg">
              {failing ? (
                <TriangleAlert aria-hidden="true" className="size-4 shrink-0 text-loss" />
              ) : (
                <CircleCheck aria-hidden="true" className="size-4 shrink-0 text-gain" />
              )}
              {w.name}
            </p>
            {failing ? (
              <>
                <p className="mt-2 text-sm text-fg">
                  Gagal ×{w.fail_count} sejak {wib(w.last_err_at)}
                </p>
                <p className="mt-1 line-clamp-2 break-all font-mono text-xs text-muted">{w.last_err}</p>
              </>
            ) : (
              <p className="mt-2 text-sm text-muted">OK · {relTime(w.last_ok)}</p>
            )}
          </li>
        );
      })}
    </ul>
  );
}
