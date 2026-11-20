import { proxyStream } from "@/lib/streamProxy";

// Server-side SSE proxy to the engine (/api/stream). The API key is added here and
// never reaches the browser. Long-lived: no caching, Node runtime, abort on disconnect.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request): Promise<Response> {
  return proxyStream(req, { HADES_API_URL: process.env.HADES_API_URL, HADES_API_KEY: process.env.HADES_API_KEY });
}
