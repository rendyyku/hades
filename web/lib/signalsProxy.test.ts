import { describe, expect, it, vi } from "vitest";
import { handleSignals } from "./signalsProxy";

const KEY = "test-secret-signals-key-do-not-leak";

function req(qs = "", method = "GET", host = "localhost:3000"): Request {
  return new Request(`http://${host}/api/signals${qs}`, { method, headers: { host } });
}

function env(f: (...a: unknown[]) => Promise<Response>) {
  return { HADES_API_URL: "http://engine:8000/", HADES_API_KEY: KEY, fetchImpl: vi.fn(f) as unknown as typeof fetch };
}

describe("handleSignals", () => {
  it("forwards validated filters with the key added server side", async () => {
    const e = env(async () => new Response(JSON.stringify([{ id: 1 }]), { status: 200 }));
    const res = await handleSignals(req("?source=ct&min_score=40&before=10&limit=20"), e);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual([{ id: 1 }]);
    const [url, init] = (e.fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("http://engine:8000/api/signals?source=ct&min_score=40&before=10&limit=20");
    expect((init as RequestInit).headers).toMatchObject({ "X-Hades-Key": KEY });
  });

  it("rejects a foreign Host before any upstream call", async () => {
    const e = env(async () => new Response("[]"));
    expect((await handleSignals(req("", "GET", "evil.example"), e)).status).toBe(403);
    expect(e.fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects bad params and non-GET", async () => {
    const e = env(async () => new Response("[]"));
    expect((await handleSignals(req("?limit=0"), e)).status).toBe(400);
    expect((await handleSignals(req("?before=x"), e)).status).toBe(400);
    expect((await handleSignals(req("", "POST"), e)).status).toBe(405);
  });

  it("never relays upstream errors or the key", async () => {
    const down = await handleSignals(req(), env(async () => new Response("bad key " + KEY, { status: 401 })));
    expect(down.status).toBe(502);
    expect(await down.text()).not.toContain(KEY);
    const leak = await handleSignals(req(), env(async () => new Response(JSON.stringify([KEY]), { status: 200 })));
    expect(leak.status).toBe(502);
    const thrown = await handleSignals(
      req(),
      env(async () => {
        throw new Error("boom " + KEY);
      }),
    );
    expect(await thrown.text()).not.toContain(KEY);
  });
});
