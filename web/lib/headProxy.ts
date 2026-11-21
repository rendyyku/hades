import "server-only";

// Generic server-side proxy to a HADES head (spec 9). Never throws, never exposes the
// key (it is only ever set on the outgoing server request), never forwards an upstream
// error body. A head that is down, hung (4 s) or answers garbage is "offline".

import { getHead, isHeadId, type HeadId } from "./heads";
import { hostAllowed, originAllowed, parseAllowList } from "./originGuard";

export const HEAD_TIMEOUT_MS = 4000;

export type HeadResult<T> =
  | { ok: true; data: T }
  | { ok: false; offline: true }
  /** The head answered with a non-2xx status; its body is dropped on purpose. */
  | { ok: false; offline: false; status: number };

export interface HeadInit {
  method?: "GET" | "POST" | "DELETE";
}

type Env = Record<string, string | undefined>;

export interface HeadDeps {
  env: Env;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

const OFFLINE = { ok: false, offline: true } as const;

function safePath(path: string): boolean {
  return /^\/api\/[^\s]*$/.test(path) && !path.includes("..") && !path.startsWith("//");
}

/** Base URL (no trailing slash) and key of a head, from env (server-only values). */
export function headTarget(headId: HeadId, env: Env): { base: string; key: string } {
  const head = getHead(headId);
  return {
    base: (env[head.baseUrlEnv] || head.defaultUrl).replace(/\/+$/, ""),
    key: env[head.keyEnv] ?? "",
  };
}

/** Pure core (env + fetch injected) so vitest can drive it. */
export async function headFetchCore<T>(
  headId: HeadId,
  path: string,
  init: HeadInit | undefined,
  deps: HeadDeps,
): Promise<HeadResult<T>> {
  if (!isHeadId(headId) || !safePath(path)) return OFFLINE;
  const { base, key } = headTarget(headId, deps.env);
  const doFetch = deps.fetchImpl ?? fetch;
  let res: Response;
  try {
    res = await doFetch(`${base}${path}`, {
      method: init?.method ?? "GET",
      headers: { "X-Hades-Key": key, Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(deps.timeoutMs ?? HEAD_TIMEOUT_MS),
    });
  } catch {
    // Network error, DNS failure or timeout. The original error is dropped on purpose.
    return OFFLINE;
  }
  if (!res.ok) {
    void res.body?.cancel().catch(() => {});
    return { ok: false, offline: false, status: res.status };
  }
  let text: string;
  try {
    text = await res.text();
  } catch {
    return OFFLINE;
  }
  // Defence in depth: a head never returns the key, but never relay it if it did.
  if (key && text.includes(key)) return OFFLINE;
  try {
    return { ok: true, data: JSON.parse(text) as T };
  } catch {
    return OFFLINE;
  }
}

/** Server-side GET/POST/DELETE to a head, keyed from process.env. */
export function headFetch<T>(headId: HeadId, path: string, init?: HeadInit): Promise<HeadResult<T>> {
  return headFetchCore<T>(headId, path, init, { env: process.env });
}

// -- follow / unfollow (POST/DELETE /api/heads/[head]/follows) --------------------------

function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...extra },
  });
}

const FOLLOW_ERRORS: Record<number, string> = {
  400: "invalid address",
  404: "not found",
  409: "follow cap reached (25)",
};

/**
 * CSRF/rebinding-guarded follow proxy: an allow-listed Origin AND Host
 * (HADES_PUBLIC_ORIGIN), a validated address, the key added here on the server.
 * POST body `{address}`; DELETE `?address=`.
 */
export async function handleHeadFollow(req: Request, headParam: string, deps: HeadDeps): Promise<Response> {
  const method = req.method.toUpperCase();
  if (method !== "POST" && method !== "DELETE") {
    return json(405, { error: "method not allowed" }, { allow: "POST, DELETE" });
  }
  if (!isHeadId(headParam) || !getHead(headParam).follows) return json(404, { error: "unknown head" });
  const head = getHead(headParam);

  const allow = parseAllowList(deps.env.HADES_PUBLIC_ORIGIN);
  if (!originAllowed(req, allow) || !hostAllowed(req, allow)) return json(403, { error: "forbidden" });

  let address: unknown;
  if (method === "POST") {
    try {
      address = ((await req.json()) ?? {}).address;
    } catch {
      return json(400, { error: "invalid json" });
    }
  } else {
    address = new URL(req.url).searchParams.get("address");
  }
  if (typeof address !== "string" || !head.address.test(address)) return json(400, { error: "invalid address" });

  const res = await headFetchCore<unknown>(head.id, `/api/follows/${address}`, { method }, deps);
  if (res.ok) return json(200, res.data);
  if (res.offline) return json(502, { error: "head offline", offline: true });
  const msg = FOLLOW_ERRORS[res.status];
  return msg ? json(res.status, { error: msg }) : json(502, { error: `head HTTP ${res.status}` });
}

// -- alerts backfill (GET /api/heads/[head]/alerts) --------------------------------------

const ALERTS_DEFAULT_LIMIT = 50;
/** The Solana engine caps /api/alerts at 200 (the HL head at 500). */
const ALERTS_MAX_LIMIT = 200;

/**
 * Read-only, Host-guarded proxy to a head's recent alerts (Cerberus reconnect backfill).
 * The key is added here; an upstream error body is never relayed.
 */
export async function handleHeadAlerts(req: Request, headParam: string, deps: HeadDeps): Promise<Response> {
  if (req.method.toUpperCase() !== "GET") return json(405, { error: "method not allowed" }, { allow: "GET" });
  if (!hostAllowed(req, parseAllowList(deps.env.HADES_PUBLIC_ORIGIN))) return json(403, { error: "forbidden" });
  if (!isHeadId(headParam)) return json(404, { error: "unknown head" });

  const raw = new URL(req.url).searchParams.get("limit");
  let limit = ALERTS_DEFAULT_LIMIT;
  if (raw !== null) {
    if (!/^\d+$/.test(raw)) return json(400, { error: "invalid limit" });
    limit = Number(raw);
    if (limit < 1 || limit > ALERTS_MAX_LIMIT) return json(400, { error: "invalid limit" });
  }

  const res = await headFetchCore<unknown>(headParam, `/api/alerts?limit=${limit}`, undefined, deps);
  if (!res.ok) return json(502, { error: res.offline ? "head offline" : `head HTTP ${res.status}`, offline: res.offline });
  if (!Array.isArray(res.data)) return json(502, { error: "invalid head response" });
  return json(200, res.data);
}
