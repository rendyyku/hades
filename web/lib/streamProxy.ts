// Core of app/api/stream/route.ts: same-origin SSE relay of the engine's /api/stream.
// - Host allow-list (DNS rebinding guard) BEFORE the upstream fetch.
// - The key is added here; upstream headers and error bodies are never relayed.
// - 502 JSON when the engine is unreachable at connect time (EventSource then backs off).

import { hostAllowed, parseAllowList } from "./originGuard";
import { type ProxyEnv, json, unreachable } from "./signalsProxy";

const DEFAULT_API_URL = "http://127.0.0.1:8000";
const CONNECT_TIMEOUT_MS = 5000;

export async function handleStream(req: Request, env: ProxyEnv): Promise<Response> {
  if (!hostAllowed(req, parseAllowList(env.HADES_PUBLIC_ORIGIN))) return json(403, { error: "forbidden" });
  const base = (env.HADES_API_URL || DEFAULT_API_URL).replace(/\/+$/, "");

  const ctl = new AbortController();
  const onAbort = () => ctl.abort();
  req.signal.addEventListener("abort", onAbort);
  const timer = setTimeout(() => ctl.abort(), CONNECT_TIMEOUT_MS);
  let res: Response;
  try {
    res = await (env.fetchImpl ?? fetch)(`${base}/api/stream`, {
      headers: { "X-Hades-Key": env.HADES_API_KEY ?? "", Accept: "text/event-stream" },
      cache: "no-store",
      signal: ctl.signal,
    });
  } catch {
    req.signal.removeEventListener("abort", onAbort);
    return unreachable();
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok || !res.body) {
    req.signal.removeEventListener("abort", onAbort);
    try {
      await res.body?.cancel();
    } catch {
      /* ignore */
    }
    return unreachable();
  }
  return new Response(res.body, {
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-store, no-transform",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    },
  });
}
