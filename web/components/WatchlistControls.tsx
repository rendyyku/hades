"use client";

import { LoaderCircle, Plus, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useId, useRef, useState, useTransition, type FormEvent } from "react";
import { shortAddr } from "@/lib/format";
import { NOTE_MAX, validateWatchInput, type WatchField } from "@/lib/watchForm";

async function errorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: unknown; detail?: unknown };
    if (typeof body.error === "string") return body.error;
    if (typeof body.detail === "string") return body.detail;
  } catch {
    /* keep the status */
  }
  return `HTTP ${res.status}`;
}

const INPUT =
  "min-h-11 w-full rounded-lg border bg-surface px-3 text-sm text-fg placeholder:text-muted transition-colors duration-200";

/** Add-to-watchlist form. Validates inline, then POSTs through the same-origin proxy. */
export function WatchAddForm() {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [address, setAddress] = useState("");
  const [note, setNote] = useState("");
  const [errors, setErrors] = useState<Partial<Record<WatchField | "form", string>>>({});
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const addressRef = useRef<HTMLInputElement>(null);
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const id = useId();
  const ids = { address: `${id}-address`, note: `${id}-note`, addrErr: `${id}-aerr`, noteErr: `${id}-nerr`, noteHint: `${id}-nhint` };

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busy) return;
    setStatus("");
    const v = validateWatchInput({ address, note });
    if (!v.ok) {
      setErrors({ [v.field]: v.message });
      (v.field === "address" ? addressRef : noteRef).current?.focus();
      return;
    }
    setErrors({});
    setBusy(true);
    const a = address.trim();
    try {
      const res = await fetch("/api/watchlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(note.trim() ? { address: a, note: note.trim() } : { address: a }),
      });
      if (!res.ok) {
        const msg = await errorMessage(res);
        if (res.status === 400 && /address/i.test(msg)) {
          setErrors({ address: "The server rejected this address." });
          addressRef.current?.focus();
        } else if (res.status === 400 && /note/i.test(msg)) {
          setErrors({ note: "The server rejected this note." });
          noteRef.current?.focus();
        } else {
          setErrors({ form: `Could not add wallet · ${msg}` });
        }
        return;
      }
      setAddress("");
      setNote("");
      setStatus(`Added ${shortAddr(a)} to the watchlist.`);
      startTransition(() => router.refresh());
    } catch {
      setErrors({ form: "Could not add wallet · network error" });
    } finally {
      setBusy(false);
    }
  }

  const noteOver = note.length > NOTE_MAX;
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4 rounded-lg border border-border bg-surface p-5 sm:p-6">
      <div className="flex flex-col gap-2">
        <label htmlFor={ids.address} className="text-xs uppercase tracking-[0.16em] text-muted">
          Wallet address
        </label>
        <input
          ref={addressRef}
          id={ids.address}
          name="address"
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          placeholder="e.g. 7xKX…gAsU"
          aria-invalid={errors.address ? true : undefined}
          aria-describedby={errors.address ? ids.addrErr : undefined}
          className={`${INPUT} font-mono ${errors.address ? "border-loss" : "border-border"}`}
        />
        {errors.address ? (
          <p id={ids.addrErr} className="text-sm text-loss">
            {errors.address}
          </p>
        ) : null}
      </div>
      <div className="flex flex-col gap-2">
        <label htmlFor={ids.note} className="text-xs uppercase tracking-[0.16em] text-muted">
          Note <span className="normal-case tracking-normal">(optional)</span>
        </label>
        <textarea
          ref={noteRef}
          id={ids.note}
          name="note"
          rows={2}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          aria-invalid={errors.note ? true : undefined}
          aria-describedby={`${ids.noteHint}${errors.note ? ` ${ids.noteErr}` : ""}`}
          className={`${INPUT} py-2 ${errors.note ? "border-loss" : "border-border"}`}
        />
        <p id={ids.noteHint} className={`font-mono text-xs tabular-nums ${noteOver ? "text-loss" : "text-muted"}`}>
          {note.length} / {NOTE_MAX}
        </p>
        {errors.note ? (
          <p id={ids.noteErr} className="text-sm text-loss">
            {errors.note}
          </p>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-4">
        <button
          type="submit"
          disabled={busy}
          aria-busy={busy}
          className="inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border border-primary bg-primary/20 px-4 text-sm text-fg transition-colors duration-200 hover:bg-primary/30 disabled:cursor-wait"
        >
          {busy ? (
            <LoaderCircle aria-hidden="true" className="size-4 motion-safe:animate-spin" />
          ) : (
            <Plus aria-hidden="true" className="size-4" />
          )}
          Add to watchlist
        </button>
        <p role="status" aria-live="polite" className={errors.form ? "text-sm text-loss" : "text-sm text-gain"}>
          {errors.form ?? status}
        </p>
      </div>
    </form>
  );
}

/** Remove one watchlist entry (DELETE via the proxy), then refresh the server list. */
export function RemoveWatchButton({ address }: { address: string }) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function remove() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/watchlist?address=${encodeURIComponent(address)}`, { method: "DELETE" });
      // Already gone is the state the user asked for.
      if (!res.ok && res.status !== 404) throw new Error(await errorMessage(res));
      startTransition(() => router.refresh());
    } catch (e) {
      setError(e instanceof Error ? e.message : "request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex flex-col items-end gap-1">
      <button
        type="button"
        onClick={remove}
        disabled={busy}
        aria-busy={busy}
        aria-label={`Remove ${shortAddr(address)} from watchlist`}
        className="inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border border-border px-3 text-sm text-fg transition-colors duration-200 hover:border-loss hover:text-loss disabled:cursor-wait"
      >
        {busy ? (
          <LoaderCircle aria-hidden="true" className="size-4 motion-safe:animate-spin" />
        ) : (
          <Trash2 aria-hidden="true" className="size-4" />
        )}
        Remove
      </button>
      <span role="status" aria-live="polite" className={error ? "text-xs text-loss" : "sr-only"}>
        {error ? `Could not remove · ${error}` : ""}
      </span>
    </span>
  );
}
