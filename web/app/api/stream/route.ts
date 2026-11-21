import { mergeHeadStreams } from "@/lib/streamMerge";

// Cerberus SSE fan-in over every head (lib/streamMerge). The Host allow-list is checked
// before any upstream fetch; per-head keys are added here and never reach the browser.
// Long-lived: no caching, Node runtime, abort on disconnect.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request): Promise<Response> {
  return mergeHeadStreams(req, { env: process.env });
}
