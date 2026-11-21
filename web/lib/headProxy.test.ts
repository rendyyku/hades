import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { handleHeadAlerts, handleHeadFollow, headFetch, headFetchCore, HEAD_TIMEOUT_MS } from "./headProxy";

const KEY = "hl-secret-key-xyz";
const ADDR = "0x" + "ab".repeat(20);
const env = { HL_API_URL: "http://hl.test/", HL_API_KEY: KEY };

function jsonRes(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("headFetchCore", () => {
  it("times out after 4 s by default", () => {
    expect(HEAD_TIMEOUT_MS).toBe(4000);
  });

  it("returns data and sends the head key server-side", async () => {
    const f = vi.fn().mockResolvedValue(jsonRes(200, [{ address: ADDR }]));
    const res = await headFetchCore("hyperliquid", "/api/wallets?limit=5", undefined, { env, fetchImpl: f });
    expect(res).toEqual({ ok: true, data: [{ address: ADDR }] });
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("http://hl.test/api/wallets?limit=5");
    expect(new Headers(init.headers).get("X-Hades-Key")).toBe(KEY);
    expect(init.cache).toBe("no-store");
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it("falls back to the head's 127.0.0.1 default URL", async () => {
    const f = vi.fn().mockResolvedValue(jsonRes(200, {}));
    await headFetchCore("hyperliquid", "/api/health", undefined, { env: { HL_API_KEY: KEY }, fetchImpl: f });
    expect(f.mock.calls[0][0]).toBe("http://127.0.0.1:8010/api/health");
  });

  it("offline: a network error returns {ok:false, offline:true} and no key leaks", async () => {
    const f = vi.fn().mockRejectedValue(new TypeError(`fetch failed X-Hades-Key: ${KEY}`));
    const res = await headFetchCore("hyperliquid", "/api/wallets", undefined, { env, fetchImpl: f });
    expect(res).toEqual({ ok: false, offline: true });
    expect(JSON.stringify(res)).not.toContain(KEY);
  });

  it("offline: a hung head times out", async () => {
    const f = vi.fn(
      (_u: string, init: RequestInit) =>
        new Promise<Response>((_r, reject) => init.signal!.addEventListener("abort", () => reject(init.signal!.reason))),
    );
    const res = await headFetchCore("hyperliquid", "/api/wallets", undefined, { env, fetchImpl: f, timeoutMs: 20 });
    expect(res).toEqual({ ok: false, offline: true });
  });

  it("offline: a non-JSON body", async () => {
    const f = vi.fn().mockResolvedValue(new Response("<html>", { status: 200 }));
    expect(await headFetchCore("hyperliquid", "/api/wallets", undefined, { env, fetchImpl: f })).toEqual({
      ok: false,
      offline: true,
    });
  });

  it("never forwards the upstream error body", async () => {
    const f = vi.fn().mockResolvedValue(jsonRes(401, { detail: `bad key ${KEY}`, secret: "upstream-detail" }));
    const res = await headFetchCore("hyperliquid", "/api/wallets", undefined, { env, fetchImpl: f });
    expect(res).toEqual({ ok: false, offline: false, status: 401 });
    expect(JSON.stringify(res)).not.toContain("upstream-detail");
    expect(JSON.stringify(res)).not.toContain(KEY);
  });

  it("refuses a 2xx body that echoes the key", async () => {
    const f = vi.fn().mockResolvedValue(jsonRes(200, { oops: KEY }));
    const res = await headFetchCore("hyperliquid", "/api/wallets", undefined, { env, fetchImpl: f });
    expect(res).toEqual({ ok: false, offline: true });
  });

  it("refuses paths outside /api/ without fetching", async () => {
    const f = vi.fn();
    for (const p of ["//evil.test/api/x", "http://evil.test/api", "/other", "/api/../x"]) {
      expect(await headFetchCore("hyperliquid", p, undefined, { env, fetchImpl: f })).toEqual({
        ok: false,
        offline: true,
      });
    }
    expect(f).not.toHaveBeenCalled();
  });
});

describe("headFetch (process.env)", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("reads the head's env vars and stays offline-safe", async () => {
    vi.stubEnv("HL_API_URL", "http://hl.env");
    vi.stubEnv("HL_API_KEY", KEY);
    const f = vi.fn().mockRejectedValue(new Error(KEY));
    vi.stubGlobal("fetch", f);
    const res = await headFetch("hyperliquid", "/api/health");
    expect(res).toEqual({ ok: false, offline: true });
    expect(f.mock.calls[0][0]).toBe("http://hl.env/api/health");
    expect(new Headers(f.mock.calls[0][1].headers).get("X-Hades-Key")).toBe(KEY);
  });
});

describe("handleHeadFollow", () => {
  const ORIGIN = "http://localhost:3000";
  function req(method: string, opts: { body?: unknown; query?: string; origin?: string | null; host?: string } = {}) {
    const headers: Record<string, string> = { host: opts.host ?? "localhost:3000" };
    if (opts.origin !== null) headers.origin = opts.origin ?? ORIGIN;
    if (opts.body !== undefined) headers["content-type"] = "application/json";
    return new Request(`http://localhost:3000/api/heads/hyperliquid/follows${opts.query ?? ""}`, {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    });
  }

  it("POST follows through the head with the key added server-side", async () => {
    const f = vi.fn().mockResolvedValue(jsonRes(200, { address: ADDR, origin: "manual" }));
    const res = await handleHeadFollow(req("POST", { body: { address: ADDR } }), "hyperliquid", { env, fetchImpl: f });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ address: ADDR, origin: "manual" });
    const [url, init] = f.mock.calls[0];
    expect(url).toBe(`http://hl.test/api/follows/${ADDR}`);
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("X-Hades-Key")).toBe(KEY);
  });

  it("DELETE unfollows", async () => {
    const f = vi.fn().mockResolvedValue(jsonRes(200, { address: ADDR, removed: true }));
    const res = await handleHeadFollow(req("DELETE", { query: `?address=${ADDR}` }), "hyperliquid", {
      env,
      fetchImpl: f,
    });
    expect(res.status).toBe(200);
    expect(f.mock.calls[0][1].method).toBe("DELETE");
  });

  it("rejects a cross-site Origin, a missing Origin and a rebinding Host", async () => {
    const f = vi.fn();
    for (const r of [
      req("POST", { body: { address: ADDR }, origin: "http://evil.test" }),
      req("POST", { body: { address: ADDR }, origin: null }),
      req("POST", { body: { address: ADDR }, host: "evil.test:3000" }),
    ]) {
      expect((await handleHeadFollow(r, "hyperliquid", { env, fetchImpl: f })).status).toBe(403);
    }
    expect(f).not.toHaveBeenCalled();
  });

  it("validates the address and the method", async () => {
    const f = vi.fn();
    expect((await handleHeadFollow(req("POST", { body: { address: "0x12" } }), "hyperliquid", { env, fetchImpl: f })).status).toBe(400);
    expect((await handleHeadFollow(req("DELETE", { query: "?address=../x" }), "hyperliquid", { env, fetchImpl: f })).status).toBe(400);
    expect((await handleHeadFollow(req("GET"), "hyperliquid", { env, fetchImpl: f })).status).toBe(405);
    expect(f).not.toHaveBeenCalled();
  });

  it("404s an unknown head or a head without follows", async () => {
    const f = vi.fn();
    expect((await handleHeadFollow(req("POST", { body: { address: ADDR } }), "nope", { env, fetchImpl: f })).status).toBe(404);
    expect((await handleHeadFollow(req("POST", { body: { address: ADDR } }), "solana", { env, fetchImpl: f })).status).toBe(404);
  });

  it("maps 409 (follow cap) without forwarding the upstream body", async () => {
    const f = vi.fn().mockResolvedValue(jsonRes(409, { detail: `cap 25 reached ${KEY}` }));
    const res = await handleHeadFollow(req("POST", { body: { address: ADDR } }), "hyperliquid", { env, fetchImpl: f });
    expect(res.status).toBe(409);
    const text = await res.text();
    expect(text).not.toContain(KEY);
    expect(JSON.parse(text)).toEqual({ error: "follow cap reached (25)" });
  });

  it("offline head is 502 head offline", async () => {
    const f = vi.fn().mockRejectedValue(new Error(KEY));
    const res = await handleHeadFollow(req("POST", { body: { address: ADDR } }), "hyperliquid", { env, fetchImpl: f });
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "head offline", offline: true });
  });
});

describe("handleHeadAlerts (Cerberus reconnect backfill)", () => {
  const PUB = { ...env, HADES_PUBLIC_ORIGIN: "http://localhost:3000" };
  const get = (query = "", host = "localhost:3000", method = "GET") =>
    new Request(`http://${host}/api/heads/hyperliquid/alerts${query}`, { method, headers: { host } });

  it("checks the Host allow-list before any upstream fetch", async () => {
    const f = vi.fn();
    const res = await handleHeadAlerts(get("", "evil.example:3000"), "hyperliquid", { env: PUB, fetchImpl: f });
    expect(res.status).toBe(403);
    expect(f).not.toHaveBeenCalled();
  });

  it("is GET only, for known heads, with a validated limit", async () => {
    const f = vi.fn();
    expect((await handleHeadAlerts(get("", "localhost:3000", "POST"), "hyperliquid", { env: PUB, fetchImpl: f })).status).toBe(405);
    expect((await handleHeadAlerts(get(), "polymarket", { env: PUB, fetchImpl: f })).status).toBe(404);
    for (const q of ["?limit=0", "?limit=201", "?limit=abc", "?limit=5%20"]) {
      expect((await handleHeadAlerts(get(q), "hyperliquid", { env: PUB, fetchImpl: f })).status).toBe(400);
    }
    expect(f).not.toHaveBeenCalled();
  });

  it("relays the head's alert array, fetched with the key server-side", async () => {
    const f = vi.fn().mockResolvedValue(jsonRes(200, [{ id: 1 }]));
    const res = await handleHeadAlerts(get("?limit=20"), "hyperliquid", { env: PUB, fetchImpl: f });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual([{ id: 1 }]);
    expect(f.mock.calls[0][0]).toBe("http://hl.test/api/alerts?limit=20");
    expect(new Headers(f.mock.calls[0][1].headers).get("X-Hades-Key")).toBe(KEY);
  });

  it("answers 502 without the upstream body or key when the head is offline, errors or sends a non-array", async () => {
    const impls = [
      vi.fn().mockRejectedValue(new Error(KEY)),
      vi.fn().mockResolvedValue(jsonRes(500, { detail: KEY })),
      vi.fn().mockResolvedValue(jsonRes(200, { detail: "x" })),
    ];
    for (const f of impls) {
      const res = await handleHeadAlerts(get(), "hyperliquid", { env: PUB, fetchImpl: f });
      expect(res.status).toBe(502);
      expect(await res.text()).not.toContain(KEY);
    }
  });
});
