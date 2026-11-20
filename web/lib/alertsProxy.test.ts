import { describe, expect, it, vi } from "vitest";
import { handleAlerts } from "./alertsProxy";

const KEY = "test-secret-alerts-key-do-not-leak";
const ROW = {
  id: 7,
  wallet: "H9SaZazTkfFYgqXoEYDzfQNVLnDLph7C65BB4vX9c9Sw",
  mint: "QDsTBqfFEt4yrpqfU7edcc9E6u7nHrbz15eqY8jpump",
  symbol: "GMGM",
  side: "buy",
  sol_amount: 1.5,
  signature: "5mWq",
  block_time: "2026-11-19T02:59:24Z",
};

function req(qs = "", method = "GET"): Request {
  return new Request(`http://localhost:3200/api/alerts${qs}`, { method });
}

function env(f: (...a: unknown[]) => Promise<Response>) {
  return {
    HADES_API_URL: "http://engine:8200/",
    HADES_API_KEY: KEY,
    HADES_PUBLIC_ORIGIN: "http://localhost:3200",
    fetchImpl: vi.fn(f) as unknown as typeof fetch,
  };
}

async function expectNoKey(res: Response) {
  const text = await res.clone().text();
  expect(text).not.toContain(KEY);
  for (const [, v] of res.headers) expect(v).not.toContain(KEY);
}

describe("handleAlerts", () => {
  it("fetches the engine alerts with the key header and the limit (default 50)", async () => {
    const e = env(async () => new Response(JSON.stringify([ROW]), { status: 200 }));
    const res = await handleAlerts(req(), e);
    const f = e.fetchImpl as unknown as ReturnType<typeof vi.fn>;
    expect(f).toHaveBeenCalledTimes(1);
    const [url, init] = f.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://engine:8200/api/alerts?limit=50");
    expect(new Headers(init.headers).get("X-Hades-Key")).toBe(KEY);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("application/json");
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(await res.json()).toEqual([ROW]);
  });

  it("forwards a valid limit", async () => {
    const e = env(async () => new Response("[]", { status: 200 }));
    await handleAlerts(req("?limit=200"), e);
    const f = e.fetchImpl as unknown as ReturnType<typeof vi.fn>;
    expect((f.mock.calls[0] as [string])[0]).toBe("http://engine:8200/api/alerts?limit=200");
  });

  it("rejects an invalid limit with 400 without calling the engine", async () => {
    for (const qs of ["?limit=0", "?limit=201", "?limit=-1", "?limit=x", "?limit=1.5"]) {
      const e = env(async () => new Response("[]", { status: 200 }));
      const res = await handleAlerts(req(qs), e);
      expect(res.status).toBe(400);
      expect(e.fetchImpl).not.toHaveBeenCalled();
    }
  });

  it("only allows GET", async () => {
    const e = env(async () => new Response("[]", { status: 200 }));
    const res = await handleAlerts(req("", "POST"), e);
    expect(res.status).toBe(405);
    expect(res.headers.get("allow")).toBe("GET");
    expect(e.fetchImpl).not.toHaveBeenCalled();
  });

  it("returns 502 on a network error without leaking the key", async () => {
    const e = env(async () => {
      throw new TypeError(`connect ECONNREFUSED ${KEY}`);
    });
    const res = await handleAlerts(req(), e);
    expect(res.status).toBe(502);
    await expectNoKey(res);
    expect(await res.json()).toEqual({ error: "engine unreachable" });
  });

  it("returns 502 on an upstream non-2xx without relaying its body", async () => {
    for (const status of [401, 404, 422, 500, 503]) {
      const e = env(async () => new Response(JSON.stringify({ detail: `bad ${KEY}` }), { status }));
      const res = await handleAlerts(req(), e);
      expect(res.status).toBe(502);
      await expectNoKey(res);
      expect(await res.json()).toEqual({ error: "engine unreachable" });
    }
  });

  it("returns 502 when the engine body is not a JSON array", async () => {
    for (const body of ["not json", "{}", '{"a":1}']) {
      const e = env(async () => new Response(body, { status: 200 }));
      const res = await handleAlerts(req(), e);
      expect(res.status).toBe(502);
    }
  });

  it("never relays a body that contains the key", async () => {
    const e = env(async () => new Response(JSON.stringify([{ ...ROW, symbol: KEY }]), { status: 200 }));
    const res = await handleAlerts(req(), e);
    expect(res.status).toBe(502);
    await expectNoKey(res);
  });
});

describe("handleAlerts host allow-list (DNS rebinding)", () => {
  it("returns 403 for a Host outside HADES_PUBLIC_ORIGIN and never calls the engine", async () => {
    const e = env(async () => new Response(JSON.stringify([ROW]), { status: 200 }));
    const r = new Request("http://rebind.example:3200/api/alerts", { headers: { host: "rebind.example:3200" } });
    const res = await handleAlerts(r, e);
    expect(res.status).toBe(403);
    expect(e.fetchImpl).not.toHaveBeenCalled();
  });
});
