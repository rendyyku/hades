import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { apiGet } from "./api";

const KEY = "test-secret-key-123";

describe("apiGet", () => {
  beforeEach(() => {
    vi.stubEnv("HADES_API_URL", "http://engine.test");
    vi.stubEnv("HADES_API_KEY", KEY);
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("test_api_error_state: network failure resolves to engine unreachable, no throw", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError(`fetch failed ${KEY}`)));
    const res = await apiGet("/api/overview");
    expect(res).toEqual({ ok: false, error: "engine unreachable" });
  });

  it("maps a non-2xx response to HTTP <status> with the status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "invalid api key" }), { status: 401 })),
    );
    const res = await apiGet("/api/overview");
    expect(res).toEqual({ ok: false, error: "HTTP 401", status: 401 });
  });

  it("returns parsed JSON on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ tracked_wallets: 3 }), { status: 200 })),
    );
    const res = await apiGet<{ tracked_wallets: number }>("/api/overview");
    expect(res).toEqual({ ok: true, data: { tracked_wallets: 3 } });
  });

  it("sends X-Hades-Key, no-store and a timeout signal to the configured URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await apiGet("/api/overview");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://engine.test/api/overview");
    expect(new Headers(init.headers).get("X-Hades-Key")).toBe(KEY);
    expect(init.cache).toBe("no-store");
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it("never leaks the key in error strings", async () => {
    const results = [];
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error(`boom ${KEY}`)));
    results.push(await apiGet("/api/overview"));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(KEY, { status: 500 })));
    results.push(await apiGet("/api/overview"));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(`not json ${KEY}`, { status: 200 })));
    results.push(await apiGet("/api/overview"));
    for (const r of results) {
      expect(r.ok).toBe(false);
      expect(JSON.stringify(r)).not.toContain(KEY);
    }
  });

  it("defaults the base URL to 127.0.0.1:8000", async () => {
    vi.stubEnv("HADES_API_URL", "");
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await apiGet("/api/overview");
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8000/api/overview");
  });
});
