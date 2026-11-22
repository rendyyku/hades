import { describe, expect, it, vi } from "vitest";
import { UNFOLLOW_WARNING, sendFollow, toggleIntent } from "./followFlow";

const ADDR = "0x" + "ab".repeat(20);

function res(status: number, body?: unknown): Response {
  return new Response(body === undefined ? "not json" : JSON.stringify(body), { status });
}

describe("toggleIntent", () => {
  it("follows at once, but never unfollows without a confirmation step", () => {
    expect(toggleIntent(false)).toBe("follow");
    expect(toggleIntent(true)).toBe("confirm-unfollow");
  });

  it("states what an unfollow costs", () => {
    expect(UNFOLLOW_WARNING).toMatch(/28-day/);
    expect(UNFOLLOW_WARNING).toMatch(/paper history/);
    expect(UNFOLLOW_WARNING).toMatch(/restart/);
    // follows.py: excluded from auto-follow only until it is re-added by hand
    expect(UNFOLLOW_WARNING).toMatch(/not be auto-followed again until you re-add it by hand/);
    expect(UNFOLLOW_WARNING).not.toMatch(/never auto-followed/);
  });
});

describe("sendFollow", () => {
  it("POSTs a follow to the same-origin proxy with the address in the body", async () => {
    const f = vi.fn().mockResolvedValue(res(200, {}));
    expect(await sendFollow("hyperliquid", ADDR, "follow", f)).toEqual({ ok: true });
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/api/heads/hyperliquid/follows");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ address: ADDR });
  });

  it("DELETEs an unfollow with the address in the query", async () => {
    const f = vi.fn().mockResolvedValue(res(200, {}));
    expect(await sendFollow("hyperliquid", ADDR, "unfollow", f)).toEqual({ ok: true });
    const [url, init] = f.mock.calls[0];
    expect(url).toBe(`/api/heads/hyperliquid/follows?address=${ADDR}`);
    expect(init.method).toBe("DELETE");
  });

  it("reports the proxy's error text, else the status, else the network failure", async () => {
    expect(await sendFollow("hyperliquid", ADDR, "follow", vi.fn().mockResolvedValue(res(409, { error: "follow cap reached (25)" })))).toEqual({
      ok: false,
      error: "follow cap reached (25)",
    });
    expect(await sendFollow("hyperliquid", ADDR, "unfollow", vi.fn().mockResolvedValue(res(502)))).toEqual({ ok: false, error: "HTTP 502" });
    expect(await sendFollow("hyperliquid", ADDR, "follow", vi.fn().mockRejectedValue(new Error("offline")))).toEqual({ ok: false, error: "offline" });
  });
});
