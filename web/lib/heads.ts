// Head registry: every HADES head (spec 9) the web app reaches through its own
// server-side proxy (lib/headProxy.ts). Env var NAMES only; values stay on the server.

export type HeadId = "solana" | "hyperliquid";

export interface Head {
  id: HeadId;
  label: string;
  /** Env var holding the head API base URL. */
  baseUrlEnv: string;
  /** Env var holding the head's X-Hades-Key (server-only). */
  keyEnv: string;
  /** Used when baseUrlEnv is unset; always 127.0.0.1. */
  defaultUrl: string;
  /** Design token name (globals.css --<accent>, Tailwind text-<accent>). */
  accent: "ember" | "hl";
  /** Web tab of this head. */
  href: string;
  /** Wallet address format accepted by the head. */
  address: RegExp;
  /** Serves POST/DELETE /api/follows/{address} (paper following). */
  follows: boolean;
}

export const HEADS: readonly Head[] = [
  {
    id: "solana",
    label: "Solana",
    baseUrlEnv: "HADES_API_URL",
    keyEnv: "HADES_API_KEY",
    defaultUrl: "http://127.0.0.1:8000",
    accent: "ember",
    href: "/wallets",
    address: /^[1-9A-HJ-NP-Za-km-z]{32,44}$/,
    follows: false,
  },
  {
    id: "hyperliquid",
    label: "Hyperliquid",
    baseUrlEnv: "HL_API_URL",
    keyEnv: "HL_API_KEY",
    defaultUrl: "http://127.0.0.1:8010",
    accent: "hl",
    href: "/hyperliquid",
    address: /^0x[0-9a-fA-F]{40}$/,
    follows: true,
  },
] as const;

export function isHeadId(id: string): id is HeadId {
  return HEADS.some((h) => h.id === id);
}

export function getHead(id: HeadId): Head {
  const h = HEADS.find((x) => x.id === id);
  if (!h) throw new Error(`unknown head: ${id}`);
  return h;
}
