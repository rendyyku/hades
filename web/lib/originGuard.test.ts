import { describe, expect, it } from "vitest";
import { DEFAULT_PUBLIC_ORIGINS, hostAllowed, originAllowed, parseAllowList } from "./originGuard";

function req(headers: Record<string, string>, url = "http://localhost:3000/api/x"): Request {
  return new Request(url, { headers });
}

describe("parseAllowList", () => {
  it("defaults to localhost:3000 and 127.0.0.1:3000", () => {
    expect(DEFAULT_PUBLIC_ORIGINS).toBe("http://localhost:3000,http://127.0.0.1:3000");
    for (const raw of [undefined, "", "  ,  "]) {
      const a = parseAllowList(raw);
      expect([...a.origins].sort()).toEqual(["http://127.0.0.1:3000", "http://localhost:3000"]);
      expect([...a.hosts].sort()).toEqual(["127.0.0.1:3000", "localhost:3000"]);
    }
  });

  it("normalises exact origins, drops junk and paths", () => {
    const a = parseAllowList(" HTTP://Example.COM:8443/ , not a url, https://hades.lan ");
    expect([...a.origins].sort()).toEqual(["http://example.com:8443", "https://hades.lan"]);
    expect([...a.hosts].sort()).toEqual(["example.com:8443", "hades.lan"]);
  });
});

describe("originAllowed / hostAllowed", () => {
  const allow = parseAllowList(undefined);

  it("accepts an allow-listed Origin and Host", () => {
    const r = req({ origin: "http://localhost:3000", host: "localhost:3000" });
    expect(originAllowed(r, allow)).toBe(true);
    expect(hostAllowed(r, allow)).toBe(true);
  });

  it("rejects a foreign, missing or null Origin", () => {
    expect(originAllowed(req({ origin: "https://evil.example", host: "localhost:3000" }), allow)).toBe(false);
    expect(originAllowed(req({ host: "localhost:3000" }), allow)).toBe(false);
    expect(originAllowed(req({ origin: "null", host: "localhost:3000" }), allow)).toBe(false);
    expect(originAllowed(req({ origin: "http://localhost:3001" }), allow)).toBe(false);
  });

  it("rejects a rebinding Host (attacker DNS name pointing at 127.0.0.1)", () => {
    expect(hostAllowed(req({ host: "evil.example:3000" }), allow)).toBe(false);
    expect(hostAllowed(req({ host: "LOCALHOST:3000" }), allow)).toBe(true);
    expect(hostAllowed(req({}, "http://evil.example:3000/api/x"), allow)).toBe(false);
  });
});
