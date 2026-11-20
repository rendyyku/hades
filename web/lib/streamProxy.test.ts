import { describe, expect, it, vi } from "vitest";
import { proxyStream } from "./streamProxy";

const KEY = "test-secret-stream-key-do-not-leak";
const ENV = { HADES_API_URL: "http://engine:8200/", HADES_API_KEY: KEY, HADES_PUBLIC_ORIGIN: "http://localhost:3200" };

function sseBody(text: string): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(c) {
      c.enqueue(new TextEncoder().encode(text));
      c.close();
    },
  });
}

function req(): Request {
  return new Request("http://localhost:3200/api/stream", { headers: { accept: "text/event-stream" } });
}

async function expectNoKey(res: Response) {
  const text = await res.text();
  expect(text).not.toContain(KEY);
  for (const [, v] of res.headers) expect(v).not.toContain(KEY);
}

function mock(impl: () => Promise<Response>) {
  return vi.fn(impl);
}

describe("proxyStream", () => {
  it("fetches the engine stream with the key header and the request signal", async () => {
    const f = mock(async () => new Response(sseBody("event: ping\ndata: {}\n\n"), { status: 200 }));
    const r = req();
    const res = await proxyStream(r, ENV, f as unknown as typeof fetch);
    expect(f).toHaveBeenCalledTimes(1);
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://engine:8200/api/stream");
    expect(new Headers(init.headers).get("X-Hades-Key")).toBe(KEY);
    expect(init.signal).toBe(r.signal);
    expect(res.status).toBe(200);
  });

  it("passes the event stream through with SSE headers", async () => {
    const body = 'event: alert\ndata: {"id":1}\n\n';
    const f = mock(
      async () =>
        new Response(sseBody(body), {
          status: 200,
          headers: { "content-type": "text/event-stream; charset=utf-8" },
        }),
    );
    const res = await proxyStream(req(), ENV, f as unknown as typeof fetch);
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    expect(res.headers.get("cache-control")).toBe("no-cache, no-transform");
    expect(res.headers.get("x-accel-buffering")).toBe("no");
    expect(await res.text()).toBe(body);
  });

  it("returns 502 JSON on a network error", async () => {
    const f = mock(async () => {
      throw new TypeError(`connect ECONNREFUSED ${KEY}`);
    });
    const res = await proxyStream(req(), ENV, f as unknown as typeof fetch);
    expect(res.status).toBe(502);
    expect(res.headers.get("content-type")).toContain("application/json");
    expect(await res.json()).toEqual({ error: "engine unreachable" });
  });

  it("returns 502 JSON on an upstream non-2xx", async () => {
    for (const status of [401, 404, 500, 503]) {
      const f = mock(async () => new Response(JSON.stringify({ detail: `bad ${KEY}` }), { status }));
      const res = await proxyStream(req(), ENV, f as unknown as typeof fetch);
      expect(res.status).toBe(502);
      expect(await res.json()).toEqual({ error: "engine unreachable" });
    }
  });

  it("returns 502 when the upstream has no body", async () => {
    const f = mock(async () => new Response(null, { status: 200 }));
    const res = await proxyStream(req(), ENV, f as unknown as typeof fetch);
    expect(res.status).toBe(502);
  });

  it("does not forward upstream headers", async () => {
    const f = mock(
      async () => new Response(sseBody(""), { status: 200, headers: { "x-hades-key": KEY, "set-cookie": "a=b" } }),
    );
    const res = await proxyStream(req(), ENV, f as unknown as typeof fetch);
    expect(res.headers.get("x-hades-key")).toBeNull();
    expect(res.headers.get("set-cookie")).toBeNull();
  });

  it("never exposes the key in any response body or header", async () => {
    const cases: (() => Promise<Response>)[] = [
      async () => new Response(sseBody("event: ping\ndata: {}\n\n"), { status: 200 }),
      async () => new Response(`denied ${KEY}`, { status: 401 }),
      async () => {
        throw new Error(KEY);
      },
    ];
    for (const impl of cases) {
      const res = await proxyStream(req(), ENV, mock(impl) as unknown as typeof fetch);
      await expectNoKey(res);
    }
  });

  it("uses the default engine URL when HADES_API_URL is unset", async () => {
    const f = mock(async () => new Response(sseBody(""), { status: 200 }));
    await proxyStream(req(), { HADES_API_KEY: KEY, HADES_PUBLIC_ORIGIN: ENV.HADES_PUBLIC_ORIGIN }, f as unknown as typeof fetch);
    expect((f.mock.calls[0] as unknown as [string])[0]).toBe("http://127.0.0.1:8000/api/stream");
  });
});

const PING = ["event: ping", "data: {}", "", ""].join(String.fromCharCode(10));

describe("proxyStream host allow-list (DNS rebinding)", () => {
  it("returns 403 for a Host outside HADES_PUBLIC_ORIGIN and never calls the engine", async () => {
    const f = vi.fn(async () => new Response(sseBody(PING), { status: 200 }));
    const r = new Request("http://evil.example:3200/api/stream", { headers: { host: "evil.example:3200" } });
    const res = await proxyStream(r, ENV, f as unknown as typeof fetch);
    expect(res.status).toBe(403);
    expect(f).not.toHaveBeenCalled();
  });

  it("uses the default allow-list (localhost:3000) when HADES_PUBLIC_ORIGIN is unset", async () => {
    const f = vi.fn(async () => new Response(sseBody(PING), { status: 200 }));
    const bad = await proxyStream(req(), { HADES_API_URL: ENV.HADES_API_URL, HADES_API_KEY: KEY }, f as unknown as typeof fetch);
    expect(bad.status).toBe(403);
    const ok = new Request("http://localhost:3000/api/stream", { headers: { host: "localhost:3000" } });
    const res = await proxyStream(ok, { HADES_API_URL: ENV.HADES_API_URL, HADES_API_KEY: KEY }, f as unknown as typeof fetch);
    expect(res.status).toBe(200);
  });
});
