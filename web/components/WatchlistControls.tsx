"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import type { DomainRow, MuteRow } from "@/lib/api";
import { wib } from "@/lib/signalView";

const FIELD =
  "min-h-11 rounded border border-border bg-bg px-3 text-sm text-fg focus-visible:outline-2 focus-visible:outline-primary";
const BTN =
  "min-h-11 cursor-pointer rounded bg-primary px-5 text-sm font-medium text-fg transition-colors duration-200 hover:bg-primary/85 disabled:cursor-not-allowed disabled:opacity-60";
const LINK_BTN = "min-h-11 cursor-pointer rounded px-3 text-sm text-muted underline underline-offset-4 hover:text-fg";

async function send(path: string, method: "POST" | "DELETE", body?: unknown): Promise<string | null> {
  try {
    const res = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.ok) return null;
    const data = (await res.json().catch(() => ({}))) as { error?: string; detail?: unknown };
    return typeof data.detail === "string" ? data.detail : (data.error ?? `HTTP ${res.status}`);
  } catch {
    return "engine unreachable";
  }
}

export function WatchlistControls({ domains, mutes }: { domains: DomainRow[]; mutes: MuteRow[] }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(path: string, method: "POST" | "DELETE", body?: unknown) {
    setBusy(true);
    setError(null);
    const err = await send(path, method, body);
    setBusy(false);
    if (err) setError(err);
    else router.refresh();
  }

  function addDomain(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const domain = String(new FormData(form).get("domain") ?? "");
    void run("/api/watchlist", "POST", { domain }).then(() => form.reset());
  }

  function addMute(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    void run("/api/mutes", "POST", { target: String(fd.get("target") ?? ""), hours: Number(fd.get("hours") || 24) });
  }

  return (
    <div className="flex flex-col gap-10">
      {error ? (
        <p role="alert" className="rounded border border-loss/40 bg-loss/10 p-3 text-sm text-fg">
          Gagal: {error}
        </p>
      ) : null}

      <div>
        <h3 className="mb-3 text-xs uppercase tracking-[0.16em] text-muted">Domain CT</h3>
        <form onSubmit={addDomain} className="mb-4 flex flex-wrap gap-3">
          <label className="sr-only" htmlFor="domain">
            Domain
          </label>
          <input id="domain" name="domain" required placeholder="projectx.xyz" className={FIELD} />
          <button type="submit" disabled={busy} className={BTN}>
            Tambah
          </button>
        </form>
        {domains.length ? (
          <ul className="divide-y divide-border rounded-lg border border-border bg-surface">
            {domains.map((d) => (
              <li key={d.domain} className="flex flex-wrap items-center justify-between gap-3 px-4 py-2">
                <span className="font-mono text-sm text-fg">{d.domain}</span>
                <span className="flex items-center gap-3">
                  <span className="rounded-full border border-border px-2 py-0.5 text-xs text-muted">{d.origin}</span>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void run(`/api/watchlist?domain=${encodeURIComponent(d.domain)}`, "DELETE")}
                    className={LINK_BTN}
                    aria-label={`Hapus ${d.domain}`}
                  >
                    Hapus
                  </button>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted">Belum ada domain.</p>
        )}
      </div>

      <div>
        <h3 className="mb-3 text-xs uppercase tracking-[0.16em] text-muted">Mute aktif</h3>
        <form onSubmit={addMute} className="mb-4 flex flex-wrap gap-3">
          <label className="sr-only" htmlFor="target">
            Target
          </label>
          <input id="target" name="target" required placeholder="monad / ct / monad:0x…" className={FIELD} />
          <label className="sr-only" htmlFor="hours">
            Jam
          </label>
          <input id="hours" name="hours" type="number" min={1} max={720} defaultValue={24} className={`${FIELD} w-24`} />
          <button type="submit" disabled={busy} className={BTN}>
            Mute
          </button>
        </form>
        {mutes.length ? (
          <ul className="divide-y divide-border rounded-lg border border-border bg-surface">
            {mutes.map((m) => (
              <li key={m.target} className="flex flex-wrap items-center justify-between gap-3 px-4 py-2">
                <span className="font-mono text-sm text-fg">{m.target}</span>
                <span className="flex items-center gap-3 text-sm text-muted">
                  sampai {wib(m.until)}
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void run(`/api/mutes?target=${encodeURIComponent(m.target)}`, "DELETE")}
                    className={LINK_BTN}
                    aria-label={`Buka mute ${m.target}`}
                  >
                    Buka
                  </button>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted">Tidak ada mute aktif.</p>
        )}
      </div>
    </div>
  );
}
