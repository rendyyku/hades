// Watchlist add-form validation. Pure; mirrors the server-side checks in watchlistProxy.

import { BASE58_ADDRESS } from "./watchlistProxy";

export const NOTE_MAX = 280;

export type WatchField = "address" | "note";
export type WatchValidation = { ok: true } | { ok: false; field: WatchField; message: string };

export function validateWatchInput({ address, note }: { address: string; note: string }): WatchValidation {
  const a = address.trim();
  if (!a) return { ok: false, field: "address", message: "Enter a wallet address." };
  if (!BASE58_ADDRESS.test(a)) {
    return {
      ok: false,
      field: "address",
      message: "Not a valid Solana address (32–44 base58 characters, no 0, O, I or l).",
    };
  }
  if (note.length > NOTE_MAX) {
    return {
      ok: false,
      field: "note",
      message: `Note must be ${NOTE_MAX} characters or fewer (${note.length}).`,
    };
  }
  return { ok: true };
}
