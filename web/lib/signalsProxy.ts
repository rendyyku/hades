// Core of app/api/signals/route.ts: a read-only GET proxy to the engine signal list (used by
// the live feed to backfill after an SSE reconnect). The key is added on the server and never
// relayed. Pure (env + fetch injected) so vitest can drive it.

import { hostAllowed, parseAllowList } from "./originGuard";
import { SOURCES } from "./signalQuery";

const DEFAULT_API_URL = "http://127.0.0.1:8000";
const TIMEOUT_MS = 8000;

export interface ProxyEnv {
  HADES_API_URL?: string;
  HADES_API_KEY?: string;
  /** Allow-listed origins; the request Host must be one of their hosts (DNS rebinding guard). */
  HADES_PUBLIC_ORIGIN?: string;
  fetchImpl?: typeof fetch;
}

export function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...extra },
  });
}

export const unreachable = () => json(502, { error: "engine unreachable" });

const VALID: Record<string, (v: string) => boolean> = {
  source: (v) => (SOURCES as readonly string[]).includes(v),
  chain: (v) => /^[a-z0-9_-]{1,32}$/.test(v),
  min_score: (v) => /^\d{1,3}$/.test(v),
  buyable: (v) => v === "true" || v === "false",
  before: (v) => /^\d{1,12}$/.test(v),
  limit: (v) => /^\d{1,3}$/.test(v) && Number(v) >= 1 && Number(v) <= 200,
};

/** GET the engine with the key; returns parsed JSON or null. Never throws, never relays bodies. */
export async function engineGet(path: string, env: ProxyEnv): Promise<unknown | null> {
  const base = (env.HADES_API_URL || DEFAULT_API_URL).replace(/\/+$/, "");
  let res: Response;
  try {
    res = await (env.fetchImpl ?? fetch)(`${base}${path}`, {
      headers: { "X-Hades-Key": env.HADES_API_KEY ?? "", Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    // The original error is dropped on purpose (it may carry URLs or headers).
    return null;
  }
  if (!res.ok) {
    try {
      await res.body?.cancel();
    } catch {
      /* ignore */
    }
    return null;
  }
  try {
    return await res.json();
  } catch {
    return null;
  }
}

/** Defence in depth: the engine never returns the key, but never relay it if it did. */
export function safeJson(data: unknown, key: string | undefined): Response {
  const text = JSON.stringify(data);
  if (key && text.includes(key)) return json(502, { error: "invalid engine response" });
  return new Response(text, { status: 200, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

export async function handleSignals(req: Request, env: ProxyEnv): Promise<Response> {
  if (req.method.toUpperCase() !== "GET") return json(405, { error: "method not allowed" }, { allow: "GET" });
  if (!hostAllowed(req, parseAllowList(env.HADES_PUBLIC_ORIGIN))) return json(403, { error: "forbidden" });

  const out = new URLSearchParams();
  for (const [k, v] of new URL(req.url).searchParams) {
    const ok = VALID[k];
    if (!ok) continue;
    if (!ok(v)) return json(400, { error: `invalid ${k}` });
    out.set(k, v);
  }
  const data = await engineGet(`/api/signals?${out.toString()}`, env);
  if (!Array.isArray(data)) return unreachable();
  return safeJson(data, env.HADES_API_KEY);
}
