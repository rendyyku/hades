// Core of app/api/stream/route.ts: proxies the engine SSE (/api/stream) to the browser
// and adds the engine key on the server. Pure (env + fetch injected) so vitest can drive it.

const DEFAULT_API_URL = "http://127.0.0.1:8000";

export interface StreamEnv {
  HADES_API_URL?: string;
  HADES_API_KEY?: string;
}

const SSE_HEADERS = {
  "content-type": "text/event-stream",
  "cache-control": "no-cache, no-transform",
  "x-accel-buffering": "no",
} as const;

function unreachable(): Response {
  return new Response(JSON.stringify({ error: "engine unreachable" }), {
    status: 502,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

export async function proxyStream(
  req: Request,
  env: StreamEnv,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  const base = (env.HADES_API_URL || DEFAULT_API_URL).replace(/\/+$/, "");
  let upstream: Response;
  try {
    upstream = await fetchImpl(`${base}/api/stream`, {
      headers: { "X-Hades-Key": env.HADES_API_KEY ?? "", Accept: "text/event-stream" },
      cache: "no-store",
      // Browser disconnect aborts the engine connection (no timeout: the stream is long-lived).
      signal: req.signal,
    });
  } catch {
    // The original error is dropped on purpose (it may carry URLs or headers).
    return unreachable();
  }
  if (!upstream.ok || !upstream.body) {
    // Release the upstream body; its content (e.g. an auth error) is never relayed.
    try {
      await upstream.body?.cancel();
    } catch {
      /* ignore */
    }
    return unreachable();
  }
  // Only the body is relayed; upstream headers are never forwarded.
  return new Response(upstream.body, { status: 200, headers: SSE_HEADERS });
}
