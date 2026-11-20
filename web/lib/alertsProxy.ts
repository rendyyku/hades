// Core of app/api/alerts/route.ts: a read-only GET proxy to the engine alerts list
// (used by LiveFeed to backfill after an SSE reconnect). The key is added on the server
// and never relayed. Pure (env + fetch injected) so vitest can drive it.

const DEFAULT_API_URL = "http://127.0.0.1:8000";
const TIMEOUT_MS = 8000;
const DEFAULT_LIMIT = 50;
const MAX_LIMIT = 200;

export interface AlertsEnv {
  HADES_API_URL?: string;
  HADES_API_KEY?: string;
  fetchImpl?: typeof fetch;
}

function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...extra },
  });
}

const unreachable = () => json(502, { error: "engine unreachable" });

export async function handleAlerts(req: Request, env: AlertsEnv): Promise<Response> {
  if (req.method.toUpperCase() !== "GET") return json(405, { error: "method not allowed" }, { allow: "GET" });

  const raw = new URL(req.url).searchParams.get("limit");
  let limit = DEFAULT_LIMIT;
  if (raw !== null) {
    if (!/^\d+$/.test(raw)) return json(400, { error: "invalid limit" });
    limit = Number(raw);
    if (limit < 1 || limit > MAX_LIMIT) return json(400, { error: "invalid limit" });
  }

  const base = (env.HADES_API_URL || DEFAULT_API_URL).replace(/\/+$/, "");
  const doFetch = env.fetchImpl ?? fetch;
  let res: Response;
  try {
    res = await doFetch(`${base}/api/alerts?limit=${limit}`, {
      headers: { "X-Hades-Key": env.HADES_API_KEY ?? "", Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    // The original error is dropped on purpose (it may carry URLs or headers).
    return unreachable();
  }
  if (!res.ok) {
    // Release the upstream body; its content (e.g. an auth error) is never relayed.
    try {
      await res.body?.cancel();
    } catch {
      /* ignore */
    }
    return unreachable();
  }

  let data: unknown;
  try {
    data = await res.json();
  } catch {
    return unreachable();
  }
  if (!Array.isArray(data)) return unreachable();
  // Defence in depth: the engine never returns the key, but never relay it if it did.
  const text = JSON.stringify(data);
  if (env.HADES_API_KEY && text.includes(env.HADES_API_KEY)) return json(502, { error: "invalid engine response" });
  return new Response(text, {
    status: 200,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}
