import { handleAlerts } from "@/lib/alertsProxy";

// Same-origin read-only proxy to the engine alerts list (LiveFeed reconnect backfill).
// The API key is added here, on the server, and never reaches the browser.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request): Promise<Response> {
  return handleAlerts(req, { HADES_API_URL: process.env.HADES_API_URL, HADES_API_KEY: process.env.HADES_API_KEY });
}
