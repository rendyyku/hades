import { describe, expect, it, vi } from "vitest";
import { handleWrite } from "./writeProxy";

const KEY = "test-secret-write-key-do-not-leak";
const ORIGIN = "http://localhost:3000";

function req(path: string, method: string, body?: unknown, origin: string | null = ORIGIN, host = "localhost:3000") {
  const headers: Record<string, string> = { host, "content-type": "application/json" };
  if (origin) headers.origin = origin;
  return new Request(`http://${host}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function env(status = 200, payload: unknown = { ok: true }) {
  const fetchImpl = vi.fn(async () => new Response(JSON.stringify(payload), { status }));
  return { env: { HADES_API_URL: "http://engine:8000", HADES_API_KEY: KEY, fetchImpl: fetchImpl as unknown as typeof fetch }, fetchImpl };
}

describe("handleWrite", () => {
  it("rejects a cross-origin POST before any upstream call", async () => {
    const { env: e, fetchImpl } = env();
    const res = await handleWrite(req("/api/watchlist", "POST", { domain: "cat.xyz" }, "https://evil.example"), e, "watchlist");
    expect(res.status).toBe(403);
    expect(fetchImpl).not.toHaveBeenCalled();
    expect((await handleWrite(req("/api/watchlist", "POST", { domain: "cat.xyz" }, null), e, "watchlist")).status).toBe(403);
    const rebind = req("/api/watchlist", "POST", { domain: "cat.xyz" }, ORIGIN, "evil.example");
    expect((await handleWrite(rebind, e, "watchlist")).status).toBe(403);
  });

  it("forwards a watchlist add with the key added server side", async () => {
    const { env: e, fetchImpl } = env(200, { domain: "cat.xyz", added: true });
    const res = await handleWrite(req("/api/watchlist", "POST", { domain: "Cat.XYZ" }), e, "watchlist");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ domain: "cat.xyz", added: true });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://engine:8000/api/watchlist");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({ "X-Hades-Key": KEY });
    expect(JSON.parse(String(init.body))).toEqual({ domain: "Cat.XYZ" });
  });

  it("builds encoded DELETE paths and validates input", async () => {
    const { env: e, fetchImpl } = env();
    await handleWrite(req("/api/mutes?target=monad%3A0xabc", "DELETE"), e, "mutes");
    expect((fetchImpl.mock.calls[0] as unknown as [string])[0]).toBe("http://engine:8000/api/mutes/monad%3A0xabc");
    expect((await handleWrite(req("/api/watchlist?domain=", "DELETE"), e, "watchlist")).status).toBe(400);
    expect((await handleWrite(req("/api/watchlist", "POST", { domain: 5 }), e, "watchlist")).status).toBe(400);
    expect((await handleWrite(req("/api/mutes", "POST", { target: "ct", hours: 0 }), e, "mutes")).status).toBe(400);
    expect((await handleWrite(req("/api/mutes", "POST", { target: "a b", hours: 2 }), e, "mutes")).status).toBe(400);
    expect((await handleWrite(req("/api/mutes", "GET"), e, "mutes")).status).toBe(405);
  });

  it("relays engine errors without the key", async () => {
    const { env: e } = env(400, { detail: "invalid domain" });
    const res = await handleWrite(req("/api/watchlist", "POST", { domain: "x" }), e, "watchlist");
    expect(res.status).toBe(400);
    const { env: leaky } = env(200, { echo: KEY });
    const bad = await handleWrite(req("/api/watchlist", "POST", { domain: "x" }), leaky, "watchlist");
    expect(bad.status).toBe(502);
    expect(await bad.text()).not.toContain(KEY);
  });
});
