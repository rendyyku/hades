import { handleHeadAlerts } from "@/lib/headProxy";

// Same-origin read-only proxy to a head's recent alerts (Cerberus reconnect backfill).
// The head key is added on the server and never reaches the browser.
export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ head: string }> };

export async function GET(req: Request, { params }: Ctx): Promise<Response> {
  return handleHeadAlerts(req, (await params).head, { env: process.env });
}
