import { handleHeadPositions } from "@/lib/headProxy";

// Same-origin read-only proxy to a head's live open positions of one wallet (polled by
// the wallet page). The head key is added on the server and never reaches the browser.
export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ head: string; address: string }> };

export async function GET(req: Request, { params }: Ctx): Promise<Response> {
  const { head, address } = await params;
  return handleHeadPositions(req, head, address, { env: process.env });
}
