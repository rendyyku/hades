import { handleSignals } from "@/lib/signalsProxy";

// Read-only proxy to the engine signal list (live feed backfill after a reconnect).
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request): Promise<Response> {
  return handleSignals(req, {
    HADES_API_URL: process.env.HADES_API_URL,
    HADES_API_KEY: process.env.HADES_API_KEY,
    HADES_PUBLIC_ORIGIN: process.env.HADES_PUBLIC_ORIGIN,
  });
}
