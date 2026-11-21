import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  applyHeadStatus,
  fetchHeadBackfill,
  filterFeed,
  mergeFeed,
  parseFeedEvent,
  parseHeadStatus,
  toFeedItems,
  type FeedItem,
} from "./live";
import { createSseParser, mergeHeadStreams, STREAM_CONNECT_TIMEOUT_MS, STREAM_IDLE_TIMEOUT_MS } from "./streamMerge";

const SOL_KEY = "sol-secret-stream-key-do-not-leak";
const HL_KEY = "hl-secret-stream-key-do-not-leak";
const ENV = {
  HADES_API_URL: "http://sol.test/",
  HADES_API_KEY: SOL_KEY,
  HL_API_URL: "http://hl.test",
  HL_API_KEY: HL_KEY,
  HADES_PUBLIC_ORIGIN: "http://localhost:3200",
};
const LF = String.fromCharCode(10);
const enc = new TextEncoder();

const SOL_ALERT = { id: 7, wallet: "W", mint: "M", symbol: null, side: "buy", sol_amount: 1.5, signature: "S", block_time: "2026-11-20T10:00:00Z" };
const HL_ALERT = { id: 7, head: "hyperliquid", kind: "open", wallet: "0x" + "ab".repeat(20), market_id: "BTC", payload: { size: "0.5" }, created_at: "2026-11-20T10:00:05Z" };

function frame(event: string, data: unknown, id?: number): string {
  return `${id != null ? `id: ${id}${LF}` : ""}event: ${event}${LF}data: ${JSON.stringify(data)}${LF}${LF}`;
}

/** A controllable upstream SSE body. */
function upstream() {
  let ctl!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      ctl = c;
    },
  });
  return {
    body,
    push: (s: string) => ctl.enqueue(enc.encode(s)),
    fail: () => ctl.error(new Error(`boom ${SOL_KEY} ${HL_KEY}`)),
  };
}

function request(host = "localhost:3200") {
  const ac = new AbortController();
  const req = new Request(`http://${host}/api/stream`, { headers: { host, accept: "text/event-stream" }, signal: ac.signal });
  return { req, abort: () => ac.abort() };
}

/** Read the merged stream until `done(text)` holds (or 2 s pass). */
async function readUntil(res: Response, done: (text: string) => boolean): Promise<string> {
  const reader = res.body!.getReader();
  const dec = new TextDecoder();
  let text = "";
  const deadline = Date.now() + 2000;
  while (!done(text) && Date.now() < deadline) {
    const r = await Promise.race([
      reader.read(),
      new Promise<{ done: true; value: undefined }>((ok) => setTimeout(() => ok({ done: true, value: undefined }), 50)),
    ]);
    if (r.value) text += dec.decode(r.value, { stream: true });
    else if (r.done && (await Promise.race([reader.closed.then(() => true), Promise.resolve(false)]))) break;
  }
  reader.releaseLock();
  return text;
}

function events(text: string) {
  const p = createSseParser();
  return p.feed(text);
}

describe("createSseParser", () => {
  it("parses events across chunk boundaries, CRLF, comments and multi-line data", () => {
    const p = createSseParser();
    expect(p.feed("event: alert\r\nda")).toEqual([]);
    expect(p.feed('ta: {"a":\r\ndata: 1}\r\n\r\n: comment\r\n\r\nevent: ping\ndata: {}\n\n')).toEqual([
      { event: "alert", data: '{"a":\n1}' },
      { event: "ping", data: "{}" },
    ]);
  });

  it("defaults the event name to message and ignores id/retry fields", () => {
    expect(createSseParser().feed(`id: 3${LF}retry: 10${LF}data: {}${LF}${LF}`)).toEqual([{ event: "message", data: "{}" }]);
  });
});

describe("mergeHeadStreams", () => {
  it("checks the Host allow-list BEFORE any upstream fetch", async () => {
    const f = vi.fn();
    const { req } = request("evil.example:3200");
    const res = await mergeHeadStreams(req, { env: ENV, fetchImpl: f as unknown as typeof fetch });
    expect(res.status).toBe(403);
    expect(f).not.toHaveBeenCalled();
  });

  it("uses the default allow-list (localhost:3000) when HADES_PUBLIC_ORIGIN is unset", async () => {
    const f = vi.fn();
    const { req } = request("localhost:3200");
    const res = await mergeHeadStreams(req, { env: { ...ENV, HADES_PUBLIC_ORIGIN: undefined }, fetchImpl: f as unknown as typeof fetch });
    expect(res.status).toBe(403);
    expect(f).not.toHaveBeenCalled();
    // The accept half: the default origin itself is allowed and reaches the heads.
    const ok = vi.fn(async () => new Response(upstream().body, { status: 200 }));
    const def = request("localhost:3000");
    const res2 = await mergeHeadStreams(def.req, { env: { ...ENV, HADES_PUBLIC_ORIGIN: undefined }, fetchImpl: ok as unknown as typeof fetch });
    expect(res2.status).toBe(200);
    expect(ok).toHaveBeenCalledTimes(2);
    def.abort();
  });

  it("sends the SSE headers (incl. x-accel-buffering: no) and no key in ANY response header", async () => {
    const f = vi.fn(
      async () => new Response(upstream().body, { status: 200, headers: { "x-hades-key": SOL_KEY, "x-upstream": HL_KEY, "set-cookie": "a=b" } }),
    );
    const { req, abort } = request();
    const res = await mergeHeadStreams(req, { env: ENV, fetchImpl: f as unknown as typeof fetch });
    expect(res.headers.get("x-accel-buffering")).toBe("no");
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    const all = [...res.headers.entries()];
    expect(all.map(([k]) => k).sort()).toEqual(["cache-control", "content-type", "x-accel-buffering"]);
    for (const [k, v] of all) {
      for (const key of [SOL_KEY, HL_KEY]) {
        expect(k).not.toContain(key);
        expect(v).not.toContain(key);
      }
    }
    abort();
  });

  it("has a 5 s connect timeout and a 45 s idle window by default (3x the HL 15 s heartbeat)", () => {
    expect(STREAM_CONNECT_TIMEOUT_MS).toBe(5_000);
    expect(STREAM_IDLE_TIMEOUT_MS).toBe(45_000);
  });

  it("streams the other head when one head accepts the connection but never answers", async () => {
    const hl = upstream();
    let solCalls = 0;
    const f = vi.fn((url: string) => {
      if (url.startsWith("http://sol.test")) {
        solCalls += 1;
        return new Promise<Response>(() => {}); // never resolves, ignores the abort signal
      }
      return Promise.resolve(new Response(hl.body, { status: 200 }));
    });
    const { req, abort } = request();
    const res = await Promise.race([
      mergeHeadStreams(req, { env: ENV, fetchImpl: f as unknown as typeof fetch, connectTimeoutMs: 30, retryMinMs: 20 }),
      new Promise<never>((_, no) => setTimeout(() => no(new Error("the merged stream is blocked by the stalled head")), 1000)),
    ]);
    expect(res.status).toBe(200);
    // The connect timeout covers the header phase only: the long-lived body outlives it.
    await new Promise((r) => setTimeout(r, 60));
    hl.push(frame("alert", HL_ALERT));
    const text = await readUntil(res, (t) => t.includes('"market_id"') && t.includes('{"head":"solana","up":false}'));
    // The stalled head is retried on its normal schedule.
    await vi.waitFor(() => expect(solCalls).toBeGreaterThanOrEqual(2));
    abort();
    const evs = events(text);
    expect(evs.filter((e) => e.event === "alert").map((e) => JSON.parse(e.data).head)).toEqual(["hyperliquid"]);
    expect(evs.filter((e) => e.event === "head").map((e) => JSON.parse(e.data))).toEqual(
      expect.arrayContaining([{ head: "solana", up: false }, { head: "hyperliquid", up: true }]),
    );
  });

  it("aborts a stalled connect attempt's upstream fetch", async () => {
    const signals: AbortSignal[] = [];
    const hl = upstream();
    const f = vi.fn((url: string, init: RequestInit) => {
      if (url.startsWith("http://sol.test")) {
        signals.push(init.signal!);
        return new Promise<Response>(() => {});
      }
      return Promise.resolve(new Response(hl.body, { status: 200 }));
    });
    const { req, abort } = request();
    await mergeHeadStreams(req, { env: ENV, fetchImpl: f as unknown as typeof fetch, connectTimeoutMs: 20, retryMinMs: 60_000 });
    expect(signals[0].aborted).toBe(true);
    abort();
  });

  it("treats a head silent beyond the idle window as down and reconnects it", async () => {
    const sol1 = upstream();
    const sol2 = upstream();
    const hl = upstream();
    const signals: AbortSignal[] = [];
    let solCalls = 0;
    const f = vi.fn(async (url: string, init: RequestInit) => {
      if (url.startsWith("http://sol.test")) {
        signals.push(init.signal!);
        solCalls += 1;
        return new Response(solCalls === 1 ? sol1.body : sol2.body, { status: 200 });
      }
      return new Response(hl.body, { status: 200 });
    });
    const { req, abort } = request();
    const res = await mergeHeadStreams(req, { env: ENV, fetchImpl: f as unknown as typeof fetch, idleTimeoutMs: 80, retryMinMs: 5 });
    // hl keeps talking (inside its idle window); sol1 says one thing and then goes silent.
    sol1.push(frame("ping", { t: "s1" }));
    const keepAlive = setInterval(() => hl.push(frame("ping", { t: "h" })), 20);
    await vi.waitFor(() => expect(solCalls).toBe(2), { timeout: 1500 });
    sol2.push(frame("alert", SOL_ALERT));
    const text = await readUntil(res, (t) => t.includes('"mint"'));
    clearInterval(keepAlive);
    abort();
    expect(signals[0].aborted).toBe(true); // the silent upstream is released
    const heads = events(text).filter((e) => e.event === "head").map((e) => JSON.parse(e.data));
    expect(heads).toEqual(expect.arrayContaining([{ head: "solana", up: false }, { head: "solana", up: true }]));
    expect(heads).not.toContainEqual({ head: "hyperliquid", up: false });
  });

  it("fetches every head's stream with its own key, URL and an abortable signal", async () => {
    const sol = upstream();
    const hl = upstream();
    const f = vi.fn(async (url: string) => new Response(url.startsWith("http://sol.test") ? sol.body : hl.body, { status: 200 }));
    const { req, abort } = request();
    const res = await mergeHeadStreams(req, { env: ENV, fetchImpl: f as unknown as typeof fetch });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    expect(res.headers.get("cache-control")).toBe("no-cache, no-transform");
    const calls = f.mock.calls as unknown as [string, RequestInit][];
    const byUrl = Object.fromEntries(calls.map(([u, i]) => [u, new Headers(i.headers).get("X-Hades-Key")]));
    expect(byUrl).toEqual({ "http://sol.test/api/stream": SOL_KEY, "http://hl.test/api/stream": HL_KEY });
    for (const [, init] of calls) expect(init.signal).toBeInstanceOf(AbortSignal);
    abort();
  });

  it("tags every event with its head and drops upstream ids", async () => {
    const sol = upstream();
    const hl = upstream();
    const f = vi.fn(async (url: string) => new Response(url.startsWith("http://sol.test") ? sol.body : hl.body, { status: 200 }));
    const { req, abort } = request();
    const res = await mergeHeadStreams(req, { env: ENV, fetchImpl: f as unknown as typeof fetch });
    sol.push(frame("alert", SOL_ALERT, 7));
    hl.push(frame("alert", { ...HL_ALERT, head: "solana" }, 7));
    hl.push(frame("ping", { t: "x" }));
    const text = await readUntil(res, (t) => t.includes('"t":"x"') && t.includes('"market_id"') && t.includes('"mint"'));
    abort();
    expect(text).not.toMatch(/^id:/m);
    const alerts = events(text).filter((e) => e.event === "alert").map((e) => JSON.parse(e.data));
    expect(alerts).toContainEqual({ ...SOL_ALERT, head: "solana" });
    // The proxy knows which head it read from: an upstream's own head field is overridden.
    expect(alerts).toContainEqual({ ...HL_ALERT, head: "hyperliquid" });
    expect(events(text).filter((e) => e.event === "ping").map((e) => JSON.parse(e.data))).toContainEqual({ t: "x", head: "hyperliquid" });
  });

  it("keeps the other heads streaming when one head's stream errors", async () => {
    const sol = upstream();
    const hl = upstream();
    const f = vi.fn(async (url: string) => new Response(url.startsWith("http://sol.test") ? sol.body : hl.body, { status: 200 }));
    const { req, abort } = request();
    const res = await mergeHeadStreams(req, { env: ENV, fetchImpl: f as unknown as typeof fetch, retryMinMs: 60_000 });
    sol.push(frame("alert", SOL_ALERT));
    await new Promise((r) => setTimeout(r, 20));
    sol.fail();
    await new Promise((r) => setTimeout(r, 20));
    hl.push(frame("alert", HL_ALERT));
    hl.push(frame("alert", { ...HL_ALERT, id: 8 }));
    const text = await readUntil(res, (t) => t.includes('"id":8'));
    abort();
    const evs = events(text);
    expect(evs.filter((e) => e.event === "alert").map((e) => JSON.parse(e.data).head)).toEqual(["solana", "hyperliquid", "hyperliquid"]);
    // The failure is reported as a head status event, never as an error body.
    expect(evs.filter((e) => e.event === "head").map((e) => JSON.parse(e.data))).toContainEqual({ head: "solana", up: false });
    expect(text).not.toContain(SOL_KEY);
    expect(text).not.toContain("boom");
  });

  it("streams the heads that are up when another head is offline at connect time, and retries it", async () => {
    const hl = upstream();
    const sol2 = upstream();
    let solCalls = 0;
    const f = vi.fn(async (url: string) => {
      if (url.startsWith("http://sol.test")) {
        solCalls += 1;
        if (solCalls === 1) throw new TypeError(`ECONNREFUSED ${SOL_KEY}`);
        return new Response(sol2.body, { status: 200 });
      }
      return new Response(hl.body, { status: 200 });
    });
    const { req, abort } = request();
    const res = await mergeHeadStreams(req, { env: ENV, fetchImpl: f as unknown as typeof fetch, retryMinMs: 5 });
    expect(res.status).toBe(200);
    hl.push(frame("alert", HL_ALERT));
    const first = await readUntil(res, (t) => t.includes('"market_id"') && t.includes('"up":true'));
    await vi.waitFor(() => expect(solCalls).toBe(2));
    sol2.push(frame("alert", SOL_ALERT));
    const rest = await readUntil(res, (t) => t.includes('"mint"'));
    abort();
    const text = first + rest;
    expect(events(text).filter((e) => e.event === "head").map((e) => JSON.parse(e.data))).toEqual(
      expect.arrayContaining([{ head: "solana", up: false }, { head: "hyperliquid", up: true }, { head: "solana", up: true }]),
    );
    expect(text).not.toContain(SOL_KEY);
  });

  it("returns 502 JSON without any key when every head is down", async () => {
    const cases: ((url: string) => Promise<Response>)[] = [
      async () => {
        throw new TypeError(`ECONNREFUSED ${SOL_KEY} ${HL_KEY}`);
      },
      async () => new Response(`denied ${SOL_KEY} ${HL_KEY}`, { status: 401 }),
      async () => new Response(null, { status: 200 }),
    ];
    for (const impl of cases) {
      const { req } = request();
      const res = await mergeHeadStreams(req, { env: ENV, fetchImpl: vi.fn(impl) as unknown as typeof fetch });
      expect(res.status).toBe(502);
      const text = await res.text();
      expect(JSON.parse(text)).toEqual({ error: "heads unreachable" });
      expect(text).not.toContain(SOL_KEY);
      expect(text).not.toContain(HL_KEY);
    }
  });

  it("never relays upstream headers, non-JSON data or data containing a key", async () => {
    const sol = upstream();
    const hl = upstream();
    const f = vi.fn(async (url: string) =>
      url.startsWith("http://sol.test")
        ? new Response(sol.body, { status: 200, headers: { "x-hades-key": SOL_KEY, "set-cookie": "a=b" } })
        : new Response(hl.body, { status: 200 }),
    );
    const { req, abort } = request();
    const res = await mergeHeadStreams(req, { env: ENV, fetchImpl: f as unknown as typeof fetch });
    expect(res.headers.get("x-hades-key")).toBeNull();
    expect(res.headers.get("set-cookie")).toBeNull();
    sol.push(`event: alert${LF}data: not json${LF}${LF}`);
    sol.push(frame("alert", { ...SOL_ALERT, symbol: HL_KEY }));
    hl.push(frame("alert", { ...HL_ALERT, wallet: SOL_KEY }));
    sol.push(frame("ping", {}));
    const text = await readUntil(res, (t) => t.includes('"ping"') || t.includes("event: ping"));
    abort();
    expect(text).not.toContain(SOL_KEY);
    expect(text).not.toContain(HL_KEY);
    expect(text).not.toContain("not json");
  });

  it("closes the merged stream and aborts the upstreams when the browser disconnects", async () => {
    const sol = upstream();
    const hl = upstream();
    const signals: AbortSignal[] = [];
    const f = vi.fn(async (url: string, init: RequestInit) => {
      signals.push(init.signal!);
      return new Response(url.startsWith("http://sol.test") ? sol.body : hl.body, { status: 200 });
    });
    const { req, abort } = request();
    const res = await mergeHeadStreams(req, { env: ENV, fetchImpl: f as unknown as typeof fetch });
    abort();
    await vi.waitFor(() => expect(signals.every((s) => s.aborted)).toBe(true));
    const reader = res.body!.getReader();
    for (;;) {
      const r = await reader.read();
      if (r.done) break;
    }
  });
});

describe("client feed merge", () => {
  const sol = parseFeedEvent(JSON.stringify({ ...SOL_ALERT, head: "solana" }))!;
  const hl = parseFeedEvent(JSON.stringify(HL_ALERT))!;

  it("parses head-tagged alerts of either head, keyed by head and id", () => {
    expect(sol).toMatchObject({ head: "solana", key: "solana:7", at: SOL_ALERT.block_time });
    expect(hl).toMatchObject({ head: "hyperliquid", key: "hyperliquid:7", at: HL_ALERT.created_at });
    expect(parseFeedEvent(JSON.stringify(SOL_ALERT))).toBeNull(); // untagged
    expect(parseFeedEvent("nope")).toBeNull();
    expect(parseFeedEvent(JSON.stringify({ ...HL_ALERT, kind: 5 }))).toBeNull();
  });

  it("does not let equal ids of different heads collide, newest first", () => {
    const merged = mergeFeed([sol], [hl]);
    expect(merged.map((i) => i.key)).toEqual(["hyperliquid:7", "solana:7"]);
    expect(mergeFeed(merged, [hl]).length).toBe(2);
  });

  it("caps the merged feed", () => {
    const many: FeedItem[] = Array.from({ length: 5 }, (_, i) => parseFeedEvent(JSON.stringify({ ...HL_ALERT, id: i }))!);
    expect(mergeFeed([], many, 3)).toHaveLength(3);
  });

  it("filters by head", () => {
    const merged = mergeFeed([sol], [hl]);
    expect(filterFeed(merged, "all")).toHaveLength(2);
    expect(filterFeed(merged, "solana").map((i) => i.head)).toEqual(["solana"]);
    expect(filterFeed(merged, "hyperliquid").map((i) => i.head)).toEqual(["hyperliquid"]);
  });

  it("reads a head's alert list (server render and reconnect backfill)", () => {
    expect(toFeedItems("solana", [SOL_ALERT, { bad: 1 }]).map((i) => i.key)).toEqual(["solana:7"]);
    expect(toFeedItems("hyperliquid", [HL_ALERT]).map((i) => i.key)).toEqual(["hyperliquid:7"]);
    expect(toFeedItems("hyperliquid", { detail: "x" })).toEqual([]);
  });

  it("parses head status events", () => {
    expect(parseHeadStatus('{"head":"solana","up":false}')).toEqual({ head: "solana", up: false });
    expect(parseHeadStatus('{"head":"nope","up":true}')).toBeNull();
  });

  it("backfills a head only when its stream comes back up after being down", () => {
    let st = applyHeadStatus({}, { head: "hyperliquid", up: true });
    expect(st).toEqual({ next: { hyperliquid: true }, backfill: null }); // first up: the server render covers it
    st = applyHeadStatus(st.next, { head: "hyperliquid", up: false });
    expect(st).toEqual({ next: { hyperliquid: false }, backfill: null });
    st = applyHeadStatus(st.next, { head: "solana", up: true });
    expect(st.backfill).toBeNull();
    st = applyHeadStatus(st.next, { head: "hyperliquid", up: true });
    expect(st).toEqual({ next: { hyperliquid: true, solana: true }, backfill: "hyperliquid" });
    expect(applyHeadStatus(st.next, { head: "hyperliquid", up: true }).backfill).toBeNull();
  });

  it("fetches one head's backfill through the same-origin proxy and dedupes by head+id", async () => {
    const f = vi.fn(async () => new Response(JSON.stringify([HL_ALERT, { ...HL_ALERT, id: 8 }]), { status: 200 }));
    const rows = await fetchHeadBackfill("hyperliquid", f as unknown as typeof fetch);
    expect(f).toHaveBeenCalledWith("/api/heads/hyperliquid/alerts?limit=50", { cache: "no-store" });
    expect(rows.map((r) => r.key)).toEqual(["hyperliquid:7", "hyperliquid:8"]);
    const live = parseFeedEvent(JSON.stringify(HL_ALERT))!;
    expect(mergeFeed([live], rows).map((r) => r.key).sort()).toEqual(["hyperliquid:7", "hyperliquid:8"]);
    // Failures are swallowed: the live stream continues.
    expect(await fetchHeadBackfill("solana", vi.fn(async () => new Response("x", { status: 502 })) as unknown as typeof fetch)).toEqual([]);
    expect(
      await fetchHeadBackfill("solana", vi.fn(async () => {
        throw new TypeError("offline");
      }) as unknown as typeof fetch),
    ).toEqual([]);
  });
});
