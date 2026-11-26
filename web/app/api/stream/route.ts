import { handleStream } from "@/lib/streamProxy";

// Same-origin SSE relay of the engine stream. Host allow-list first; the key is added on the
// server and never reaches the browser. Long-lived: no caching, Node runtime.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request): Promise<Response> {
  return handleStream(req, {
    HADES_API_URL: process.env.HADES_API_URL,
    HADES_API_KEY: process.env.HADES_API_KEY,
    HADES_PUBLIC_ORIGIN: process.env.HADES_PUBLIC_ORIGIN,
  });
}
