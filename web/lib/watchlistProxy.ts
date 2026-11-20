// Core of app/api/watchlist/route.ts: a CSRF/rebinding-guarded proxy that adds the
// engine key on the server. Pure (env + fetch injected) so vitest can drive it.

import { hostAllowed, originAllowed, parseAllowList } from "./originGuard";

export const BASE58_ADDRESS = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const DEFAULT_API_URL = "http://127.0.0.1:8000";
const TIMEOUT_MS = 8000;

export interface WatchlistEnv {
  HADES_API_URL?: string;
  HADES_API_KEY?: string;
  /** Comma-separated exact origins allowed to mutate (default localhost:3000 / 127.0.0.1:3000). */
  HADES_PUBLIC_ORIGIN?: string;
  fetchImpl?: typeof fetch;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

/** Mutations need an allow-listed Origin (CSRF) AND an allow-listed Host (DNS rebinding). */
function trusted(req: Request, env: WatchlistEnv): boolean {
  const allow = parseAllowList(env.HADES_PUBLIC_ORIGIN);
  return originAllowed(req, allow) && hostAllowed(req, allow);
}

export async function handleWatchlist(req: Request, env: WatchlistEnv): Promise<Response> {
  const method = req.method.toUpperCase();
  if (method !== "POST" && method !== "DELETE") {
    return new Response(JSON.stringify({ error: "method not allowed" }), {
      status: 405,
      headers: { "content-type": "application/json", allow: "POST, DELETE" },
    });
  }
  if (!trusted(req, env)) return json(403, { error: "forbidden" });

  let enginePath: string;
  let body: string | undefined;
  if (method === "POST") {
    let payload: unknown;
    try {
      payload = await req.json();
    } catch {
      return json(400, { error: "invalid json" });
    }
    const p = (payload ?? {}) as { address?: unknown; note?: unknown };
    if (typeof p.address !== "string" || !BASE58_ADDRESS.test(p.address)) {
      return json(400, { error: "invalid address" });
    }
    if (p.note !== undefined && p.note !== null && (typeof p.note !== "string" || p.note.length > 280)) {
      return json(400, { error: "invalid note" });
    }
    enginePath = "/api/watchlist";
    body = JSON.stringify(p.note == null ? { address: p.address } : { address: p.address, note: p.note });
  } else {
    const address = new URL(req.url).searchParams.get("address") ?? "";
    if (!BASE58_ADDRESS.test(address)) return json(400, { error: "invalid address" });
    enginePath = `/api/watchlist/${address}`;
  }

  const base = (env.HADES_API_URL || DEFAULT_API_URL).replace(/\/+$/, "");
  const doFetch = env.fetchImpl ?? fetch;
  let res: Response;
  try {
    res = await doFetch(`${base}${enginePath}`, {
      method,
      headers: {
        "X-Hades-Key": env.HADES_API_KEY ?? "",
        Accept: "application/json",
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    // The original error is dropped on purpose (it may carry URLs or headers).
    return json(502, { error: "engine unreachable" });
  }

  if (res.status === 204) return new Response(null, { status: 204 });
  let data: unknown;
  try {
    data = await res.json();
  } catch {
    data = res.ok ? {} : { error: `HTTP ${res.status}` };
  }
  // Defence in depth: the engine never returns the key, but never relay it if it did.
  const text = JSON.stringify(data);
  if (env.HADES_API_KEY && text.includes(env.HADES_API_KEY)) {
    return json(502, { error: "invalid engine response" });
  }
  return json(res.status, data);
}
