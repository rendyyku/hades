import { describe, expect, it, vi } from "vitest";
import { handleStream } from "./streamProxy";

const KEY = "test-secret-stream-key-do-not-leak";

function req(host = "localhost:3000"): Request {
  return new Request(`http://${host}/api/stream`, { headers: { host } });
}

describe("handleStream", () => {
  it("pipes the engine SSE body with the key added server side", async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response("event: ping\ndata: {}\n\n", {
          status: 200,
          headers: { "content-type": "text/event-stream", "x-upstream": "secret" },
        }),
    );
    const res = await handleStream(req(), {
      HADES_API_URL: "http://engine:8000",
      HADES_API_KEY: KEY,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    expect(res.headers.get("x-upstream")).toBeNull();
    expect(await res.text()).toContain("event: ping");
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://engine:8000/api/stream");
    expect(init.headers).toMatchObject({ "X-Hades-Key": KEY });
  });

  it("403 on a foreign Host, 502 when the engine is down", async () => {
    const fetchImpl = vi.fn(async () => new Response("no", { status: 503 }));
    const env = { HADES_API_KEY: KEY, fetchImpl: fetchImpl as unknown as typeof fetch };
    expect((await handleStream(req("evil.example"), env)).status).toBe(403);
    expect(fetchImpl).not.toHaveBeenCalled();
    const down = await handleStream(req(), env);
    expect(down.status).toBe(502);
    expect(await down.text()).not.toContain(KEY);
  });
});
