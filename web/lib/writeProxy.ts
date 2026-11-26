// Core of app/api/watchlist/route.ts and app/api/mutes/route.ts: CSRF/rebinding-guarded
// POST/DELETE proxy that adds the engine key on the server. Pure (env + fetch injected).

import { hostAllowed, originAllowed, parseAllowList } from "./originGuard";
import { type ProxyEnv, json } from "./signalsProxy";

const DEFAULT_API_URL = "http://127.0.0.1:8000";
const TIMEOUT_MS = 8000;
const TARGET = /^[A-Za-z0-9_.:/-]{1,200}$/;
const MAX_MUTE_H = 720;

export type WriteKind = "watchlist" | "mutes";

/** Mutations need an allow-listed Origin (CSRF) AND an allow-listed Host (DNS rebinding). */
function trusted(req: Request, env: ProxyEnv): boolean {
  const allow = parseAllowList(env.HADES_PUBLIC_ORIGIN);
  return originAllowed(req, allow) && hostAllowed(req, allow);
}

type Built = { path: string; body?: string } | { error: string };

async function build(req: Request, method: string, kind: WriteKind): Promise<Built> {
  if (method === "DELETE") {
    const param = kind === "watchlist" ? "domain" : "target";
    const v = new URL(req.url).searchParams.get(param) ?? "";
    if (!TARGET.test(v)) return { error: `invalid ${param}` };
    return { path: `/api/${kind}/${encodeURIComponent(v)}` };
  }
  let p: Record<string, unknown>;
  try {
    p = ((await req.json()) ?? {}) as Record<string, unknown>;
  } catch {
    return { error: "invalid json" };
  }
  if (kind === "watchlist") {
    // the engine normalises and validates the hostname; here only the shape is checked
    if (typeof p.domain !== "string" || !p.domain.trim() || p.domain.length > 253) return { error: "invalid domain" };
    return { path: "/api/watchlist", body: JSON.stringify({ domain: p.domain }) };
  }
  const hours = p.hours ?? 24;
  if (typeof p.target !== "string" || !TARGET.test(p.target)) return { error: "invalid target" };
  if (typeof hours !== "number" || !Number.isInteger(hours) || hours < 1 || hours > MAX_MUTE_H) {
    return { error: "invalid hours" };
  }
  return { path: "/api/mutes", body: JSON.stringify({ target: p.target, hours }) };
}

export async function handleWrite(req: Request, env: ProxyEnv, kind: WriteKind): Promise<Response> {
  const method = req.method.toUpperCase();
  if (method !== "POST" && method !== "DELETE") return json(405, { error: "method not allowed" }, { allow: "POST, DELETE" });
  if (!trusted(req, env)) return json(403, { error: "forbidden" });

  const b = await build(req, method, kind);
  if ("error" in b) return json(400, { error: b.error });

  const base = (env.HADES_API_URL || DEFAULT_API_URL).replace(/\/+$/, "");
  let res: Response;
  try {
    res = await (env.fetchImpl ?? fetch)(`${base}${b.path}`, {
      method,
      headers: {
        "X-Hades-Key": env.HADES_API_KEY ?? "",
        Accept: "application/json",
        ...(b.body ? { "Content-Type": "application/json" } : {}),
      },
      body: b.body,
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    // The original error is dropped on purpose (it may carry URLs or headers).
    return json(502, { error: "engine unreachable" });
  }
  let data: unknown;
  try {
    data = await res.json();
  } catch {
    data = res.ok ? {} : { error: `HTTP ${res.status}` };
  }
  // Defence in depth: the engine never returns the key, but never relay it if it did.
  if (env.HADES_API_KEY && JSON.stringify(data).includes(env.HADES_API_KEY)) {
    return json(502, { error: "invalid engine response" });
  }
  return json(res.status, data);
}
