import "server-only";

// Core of app/api/stream/route.ts (Cerberus): fans in the SSE stream of every head into
// one same-origin stream for the browser.
// - Host allow-list (DNS rebinding guard) BEFORE any upstream fetch (ruling P6).
// - Each head is fetched with its own key, on the server; keys, upstream headers, ids
//   and error bodies are never relayed.
// - Every relayed event is a JSON object tagged with `head` (the head it was read from).
// - One head's stream failing never closes the others: it is reported as a `head`
//   status event and retried with backoff while the browser stays connected.
// - A head that never answers (connect timeout, 5 s) or goes silent mid-stream (idle
//   watchdog, 45 s = 3x heartbeat) is treated as down and retried; others stream on.
// - 502 JSON only when no head is reachable at connect time (EventSource then backs off).

import { headTarget } from "./headProxy";
import { HEADS, type HeadId } from "./heads";
import { hostAllowed, parseAllowList } from "./originGuard";

export interface SseEvent {
  event: string;
  data: string;
}

/** Incremental SSE parser (CRLF/LF, comments, multi-line data; id/retry ignored). */
export function createSseParser() {
  let buf = "";
  let event = "";
  let data: string[] = [];
  return {
    feed(chunk: string): SseEvent[] {
      buf += chunk;
      const out: SseEvent[] = [];
      for (;;) {
        const nl = buf.search(/\r\n|\r|\n/);
        if (nl < 0) break;
        // A lone trailing \r may be the first half of \r\n: wait for the next chunk.
        if (buf[nl] === "\r" && nl === buf.length - 1) break;
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + (buf.startsWith("\r\n", nl) ? 2 : 1));
        if (line === "") {
          if (data.length) out.push({ event: event || "message", data: data.join("\n") });
          event = "";
          data = [];
          continue;
        }
        if (line.startsWith(":")) continue;
        const i = line.indexOf(":");
        const field = i < 0 ? line : line.slice(0, i);
        let value = i < 0 ? "" : line.slice(i + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "event") event = value;
        else if (field === "data") data.push(value);
      }
      return out;
    },
  };
}

type Env = Record<string, string | undefined>;

export interface StreamDeps {
  env: Env;
  fetchImpl?: typeof fetch;
  /** First reconnect delay of a failed head (doubles up to retryMaxMs). */
  retryMinMs?: number;
  retryMaxMs?: number;
  /** Max wait for a head's response headers (the connect step only, not the body). */
  connectTimeoutMs?: number;
  /** A head silent (no bytes, not even a ping) this long mid-stream is treated as down. */
  idleTimeoutMs?: number;
}

/** Connect step only: a head that never answers is down, the others stream on. */
export const STREAM_CONNECT_TIMEOUT_MS = 5_000;
/** 3x the heads' 15 s heartbeat (HL HEARTBEAT_S, Solana ping). */
export const STREAM_IDLE_TIMEOUT_MS = 45_000;
const RETRY_MIN_MS = 2_000;
const RETRY_MAX_MS = 30_000;
const EVENT_NAME = /^[a-z_]{1,32}$/;

const SSE_HEADERS = {
  "content-type": "text/event-stream",
  "cache-control": "no-cache, no-transform",
  "x-accel-buffering": "no",
} as const;

function jsonRes(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** The upstream event as a head-tagged frame; null when it must not be relayed. */
function tag(head: HeadId, ev: SseEvent, keys: string[]): string | null {
  if (!EVENT_NAME.test(ev.event) || ev.event === "head") return null;
  // Defence in depth: a head never sends a key, but never relay one if it did.
  if (keys.some((k) => k && ev.data.includes(k))) return null;
  let v: unknown;
  try {
    v = JSON.parse(ev.data);
  } catch {
    return null;
  }
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  return frame(ev.event, { ...(v as Record<string, unknown>), head });
}

interface Upstream {
  body: ReadableStream<Uint8Array>;
  /** Abort this attempt's upstream fetch (idle watchdog) and drop the link to `signal`. */
  release: () => void;
}

async function openHead(head: HeadId, deps: StreamDeps, signal: AbortSignal): Promise<Upstream | null> {
  if (signal.aborted) return null;
  const { base, key } = headTarget(head, deps.env);
  const doFetch = deps.fetchImpl ?? fetch;
  // Per-attempt controller: aborted by the browser disconnect, the connect timeout or
  // the idle watchdog; the long-lived body itself has no timeout.
  const attempt = new AbortController();
  const onAbort = () => attempt.abort();
  signal.addEventListener("abort", onAbort, { once: true });
  const release = () => {
    signal.removeEventListener("abort", onAbort);
    attempt.abort();
  };
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timedOut = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), deps.connectTimeoutMs ?? STREAM_CONNECT_TIMEOUT_MS);
  });
  let res: Response | null;
  try {
    const pending = doFetch(`${base}/api/stream`, {
      headers: { "X-Hades-Key": key, Accept: "text/event-stream" },
      cache: "no-store",
      signal: attempt.signal,
    });
    // Raced, so even a fetch that ignores its signal cannot hold the merged stream.
    res = await Promise.race([pending, timedOut]);
    if (res === null) {
      // Timed out: a late response (if the fetch ignored the abort) is released unread.
      pending.then(
        (late) => late.body?.cancel().catch(() => {}),
        () => {},
      );
    }
  } catch {
    // The original error is dropped on purpose (it may carry URLs or headers).
    res = null;
  } finally {
    clearTimeout(timer);
  }
  if (!res || !res.ok || !res.body) {
    // Release the body; its content (e.g. an auth error) is never relayed.
    try {
      await res?.body?.cancel();
    } catch {
      /* ignore */
    }
    release();
    return null;
  }
  return { body: res.body, release };
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const t = setTimeout(done, ms);
    function done() {
      clearTimeout(t);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done, { once: true });
  });
}

const IDLE = Symbol("idle");

/** Relay one head until its stream ends, errors or goes idle (resolves, never throws). */
async function pump(head: HeadId, up: Upstream, send: (s: string) => void, keys: string[], idleMs: number) {
  const reader = up.body.getReader();
  const dec = new TextDecoder();
  const parser = createSseParser();
  try {
    for (;;) {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const idle = new Promise<typeof IDLE>((resolve) => {
        timer = setTimeout(() => resolve(IDLE), idleMs);
      });
      const r = await Promise.race([reader.read(), idle]).finally(() => clearTimeout(timer));
      // Silent past its heartbeat window: treat the head as down and reconnect it.
      if (r === IDLE || r.done) break;
      for (const ev of parser.feed(dec.decode(r.value, { stream: true }))) {
        const out = tag(head, ev, keys);
        if (out) send(out);
      }
    }
  } catch {
    /* this head's stream errored: only this head is affected */
  } finally {
    up.release();
    reader.cancel().catch(() => {
      /* ignore */
    });
  }
}

export async function mergeHeadStreams(req: Request, deps: StreamDeps): Promise<Response> {
  if (!hostAllowed(req, parseAllowList(deps.env.HADES_PUBLIC_ORIGIN))) return jsonRes(403, { error: "forbidden" });

  const ctrl = new AbortController();
  const onAbort = () => ctrl.abort();
  if (req.signal.aborted) ctrl.abort();
  else req.signal.addEventListener("abort", onAbort, { once: true });
  const { signal } = ctrl;

  const heads = HEADS.map((h) => h.id);
  const keys = HEADS.map((h) => deps.env[h.keyEnv] ?? "").filter(Boolean);
  const first = await Promise.all(heads.map((h) => openHead(h, deps, signal)));
  if (first.every((b) => b === null)) {
    ctrl.abort();
    req.signal.removeEventListener("abort", onAbort);
    return jsonRes(502, { error: "heads unreachable" });
  }

  const min = deps.retryMinMs ?? RETRY_MIN_MS;
  const max = deps.retryMaxMs ?? RETRY_MAX_MS;
  const idle = deps.idleTimeoutMs ?? STREAM_IDLE_TIMEOUT_MS;
  const enc = new TextEncoder();
  let closed = false;

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const send = (s: string) => {
        if (closed) return;
        try {
          controller.enqueue(enc.encode(s));
        } catch {
          /* the browser went away */
        }
      };
      const runHead = async (head: HeadId, initial: Upstream | null) => {
        let body = initial;
        let backoff = min;
        while (!signal.aborted) {
          if (body) {
            send(frame("head", { head, up: true }));
            backoff = min;
            await pump(head, body, send, keys, idle);
          }
          if (signal.aborted) return;
          send(frame("head", { head, up: false }));
          await sleep(backoff, signal);
          backoff = Math.min(backoff * 2, max);
          if (signal.aborted) return;
          body = await openHead(head, deps, signal);
        }
      };
      signal.addEventListener(
        "abort",
        () => {
          if (closed) return;
          closed = true;
          try {
            controller.close();
          } catch {
            /* ignore */
          }
        },
        { once: true },
      );
      heads.forEach((h, i) => void runHead(h, first[i]));
    },
    cancel() {
      closed = true;
      ctrl.abort();
    },
  });
  // Only the merged body is sent; upstream headers are never forwarded.
  return new Response(stream, { status: 200, headers: SSE_HEADERS });
}
