// Origin / Host allow-list for the Next.js server routes (HADES_PUBLIC_ORIGIN).
//
// The web app binds to 127.0.0.1, but a page on another site can still reach it:
// - CSRF: a cross-site form/fetch carries that site's Origin -> mutations need an
//   allow-listed Origin.
// - DNS rebinding: an attacker name resolving to 127.0.0.1 makes the browser send
//   Host: attacker.example -> every proxy needs an allow-listed Host.

export const DEFAULT_PUBLIC_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000";

export interface AllowList {
  /** Exact origins, e.g. "http://localhost:3000". */
  origins: Set<string>;
  /** host[:port] of those origins, e.g. "localhost:3000". */
  hosts: Set<string>;
}

function parse(raw: string): AllowList {
  const origins = new Set<string>();
  const hosts = new Set<string>();
  for (const part of raw.split(",")) {
    const s = part.trim();
    if (!s) continue;
    let u: URL;
    try {
      u = new URL(s);
    } catch {
      continue;
    }
    if (u.protocol !== "http:" && u.protocol !== "https:") continue;
    origins.add(u.origin.toLowerCase());
    hosts.add(u.host.toLowerCase());
  }
  return { origins, hosts };
}

/** Comma-separated exact origins; empty / all-invalid falls back to the localhost default. */
export function parseAllowList(raw: string | undefined): AllowList {
  const list = parse(raw ?? "");
  return list.origins.size ? list : parse(DEFAULT_PUBLIC_ORIGINS);
}

/** The request Origin header is one of the allow-listed origins ("null" / missing never is). */
export function originAllowed(req: Request, allow: AllowList): boolean {
  const origin = req.headers.get("origin");
  if (!origin || origin === "null") return false;
  let normalised: string;
  try {
    normalised = new URL(origin).origin.toLowerCase();
  } catch {
    return false;
  }
  return allow.origins.has(normalised);
}

/** The Host header (else the request URL host) is one of the allow-listed hosts. */
export function hostAllowed(req: Request, allow: AllowList): boolean {
  let host = req.headers.get("host");
  if (!host) {
    try {
      host = new URL(req.url).host;
    } catch {
      return false;
    }
  }
  return allow.hosts.has(host.trim().toLowerCase());
}
