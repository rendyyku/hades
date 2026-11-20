import { describe, expect, it, vi } from "vitest";
import { handleWatchlist } from "./watchlistProxy";

const KEY = "test-secret-key-do-not-leak";
const ADDR = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU";

function env(fetchImpl: typeof fetch) {
  return { HADES_API_URL: "http://engine:8000/", HADES_API_KEY: KEY, fetchImpl };
}

function req(
  method: string,
  opts: { origin?: string | null; host?: string; body?: unknown; query?: string } = {},
): Request {
  const headers = new Headers({ "content-type": "application/json" });
  if (opts.origin !== null) headers.set("origin", opts.origin ?? "http://localhost:3000");
  headers.set("host", opts.host ?? "localhost:3000");
  return new Request(`http://localhost:3000/api/watchlist${opts.query ?? ""}`, {
    method,
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
}

function engineJson(status: number, body: unknown) {
  return vi.fn(async () =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
}

describe("handleWatchlist", () => {
  it("forwards POST to the engine with the key header", async () => {
    const f = engineJson(201, { address: ADDR, note: "x", added_at: "2026-11-20T00:00:00Z" });
    const res = await handleWatchlist(req("POST", { body: { address: ADDR, note: "x" } }), env(f));
    expect(res.status).toBe(201);
    expect(await res.json()).toMatchObject({ address: ADDR });
    expect(f).toHaveBeenCalledTimes(1);
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://engine:8000/api/watchlist");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("X-Hades-Key")).toBe(KEY);
    expect(JSON.parse(String(init.body))).toEqual({ address: ADDR, note: "x" });
  });

  it("rejects a foreign Origin with 403", async () => {
    const f = engineJson(201, {});
    const res = await handleWatchlist(
      req("POST", { origin: "https://evil.example", body: { address: ADDR } }),
      env(f),
    );
    expect(res.status).toBe(403);
    expect(f).not.toHaveBeenCalled();
  });

  it("rejects a missing Origin with 403", async () => {
    const f = engineJson(201, {});
    const post = await handleWatchlist(req("POST", { origin: null, body: { address: ADDR } }), env(f));
    expect(post.status).toBe(403);
    const del = await handleWatchlist(req("DELETE", { origin: null, query: `?address=${ADDR}` }), env(f));
    expect(del.status).toBe(403);
    expect(f).not.toHaveBeenCalled();
  });

  it("rejects an Origin with a different port", async () => {
    const f = engineJson(201, {});
    const res = await handleWatchlist(
      req("POST", { origin: "http://localhost:3001", body: { address: ADDR } }),
      env(f),
    );
    expect(res.status).toBe(403);
  });

  it("returns 400 for a bad address", async () => {
    const f = engineJson(201, {});
    for (const bad of ["0OIl-not-base58", "short", "", 42]) {
      const res = await handleWatchlist(req("POST", { body: { address: bad } }), env(f));
      expect(res.status).toBe(400);
    }
    const del = await handleWatchlist(req("DELETE", { query: "?address=../../etc" }), env(f));
    expect(del.status).toBe(400);
    expect(f).not.toHaveBeenCalled();
  });

  it("returns 400 for an invalid JSON body", async () => {
    const f = engineJson(201, {});
    const r = new Request("http://localhost:3000/api/watchlist", {
      method: "POST",
      headers: { origin: "http://localhost:3000", host: "localhost:3000" },
      body: "{not json",
    });
    expect((await handleWatchlist(r, env(f))).status).toBe(400);
  });

  it("maps DELETE to the engine path and returns 204", async () => {
    const f = vi.fn(async () => new Response(null, { status: 204 }));
    const res = await handleWatchlist(req("DELETE", { query: `?address=${ADDR}` }), env(f));
    expect(res.status).toBe(204);
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`http://engine:8000/api/watchlist/${ADDR}`);
    expect(init.method).toBe("DELETE");
    expect(new Headers(init.headers).get("X-Hades-Key")).toBe(KEY);
  });

  it("passes engine errors through with their status", async () => {
    const f = engineJson(404, { detail: "not in watchlist" });
    const res = await handleWatchlist(req("DELETE", { query: `?address=${ADDR}` }), env(f));
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ detail: "not in watchlist" });
  });

  it("returns 502 when the engine is unreachable", async () => {
    const f = vi.fn(async () => {
      throw new TypeError(`fetch failed: ${KEY}`);
    });
    const res = await handleWatchlist(req("POST", { body: { address: ADDR } }), env(f));
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "engine unreachable" });
  });

  it("rejects other methods with 405", async () => {
    const f = engineJson(200, {});
    const res = await handleWatchlist(req("PUT", { body: { address: ADDR } }), env(f));
    expect(res.status).toBe(405);
  });

  it("never echoes the key in any response body", async () => {
    const cases: [Request, typeof fetch][] = [
      [req("POST", { body: { address: ADDR } }), engineJson(201, { address: ADDR })],
      [req("POST", { origin: "https://evil.example", body: { address: ADDR } }), engineJson(201, {})],
      [req("POST", { origin: null, body: { address: ADDR } }), engineJson(201, {})],
      [req("POST", { body: { address: "bad" } }), engineJson(201, {})],
      [req("DELETE", { query: `?address=${ADDR}` }), engineJson(404, { detail: "nope" })],
      [req("POST", { body: { address: ADDR } }), vi.fn(async () => { throw new Error(KEY); })],
      [req("POST", { body: { address: ADDR } }), engineJson(401, { detail: "invalid api key" })],
    ];
    for (const [r, f] of cases) {
      const res = await handleWatchlist(r, env(f));
      const text = await res.text();
      expect(text).not.toContain(KEY);
      for (const [, v] of res.headers) expect(v).not.toContain(KEY);
    }
  });
});

describe("handleWatchlist origin allow-list (HADES_PUBLIC_ORIGIN)", () => {
  it("accepts 127.0.0.1:3000 from the default list", async () => {
    const f = engineJson(201, { address: ADDR, note: null, added_at: null });
    const res = await handleWatchlist(
      req("POST", { origin: "http://127.0.0.1:3000", host: "127.0.0.1:3000", body: { address: ADDR } }),
      env(f),
    );
    expect(res.status).toBe(201);
  });

  it("rejects an allow-listed Origin with a rebinding Host", async () => {
    const f = engineJson(201, {});
    const res = await handleWatchlist(
      req("POST", { origin: "http://localhost:3000", host: "evil.example:3000", body: { address: ADDR } }),
      env(f),
    );
    expect(res.status).toBe(403);
    expect(f).not.toHaveBeenCalled();
  });

  it("rejects same-origin requests from an origin that is not allow-listed", async () => {
    const f = engineJson(201, {});
    const res = await handleWatchlist(
      req("POST", { origin: "http://hades.lan:3000", host: "hades.lan:3000", body: { address: ADDR } }),
      env(f),
    );
    expect(res.status).toBe(403);
  });

  it("honours a custom HADES_PUBLIC_ORIGIN", async () => {
    const f = engineJson(201, { address: ADDR, note: null, added_at: null });
    const res = await handleWatchlist(
      req("POST", { origin: "http://hades.lan:3000", host: "hades.lan:3000", body: { address: ADDR } }),
      { ...env(f), HADES_PUBLIC_ORIGIN: "http://hades.lan:3000" },
    );
    expect(res.status).toBe(201);
  });
});
