import "server-only";

// Server-side client for the HADES engine API. Never throws; never exposes the key.

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string; status?: number };

const DEFAULT_API_URL = "http://127.0.0.1:8000";
const TIMEOUT_MS = 8000;

export async function apiGet<T>(path: string): Promise<ApiResult<T>> {
  const base = (process.env.HADES_API_URL || DEFAULT_API_URL).replace(/\/+$/, "");
  let res: Response;
  try {
    res = await fetch(`${base}${path}`, {
      headers: { "X-Hades-Key": process.env.HADES_API_KEY ?? "", Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    // Network error, DNS failure or timeout. The original error is dropped on purpose.
    return { ok: false, error: "engine unreachable" };
  }
  if (!res.ok) {
    return { ok: false, error: `HTTP ${res.status}`, status: res.status };
  }
  try {
    return { ok: true, data: (await res.json()) as T };
  } catch {
    return { ok: false, error: "invalid response", status: res.status };
  }
}

// -- API shapes (mirror engine/hades/api/schemas.py; datetimes are ISO 8601 strings) --
