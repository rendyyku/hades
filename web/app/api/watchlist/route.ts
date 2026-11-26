import { handleWrite } from "@/lib/writeProxy";

// CSRF + DNS-rebinding guarded write proxy; the engine key is added on the server only.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function env() {
  return {
    HADES_API_URL: process.env.HADES_API_URL,
    HADES_API_KEY: process.env.HADES_API_KEY,
    HADES_PUBLIC_ORIGIN: process.env.HADES_PUBLIC_ORIGIN,
  };
}

export async function POST(req: Request): Promise<Response> {
  return handleWrite(req, env(), "watchlist");
}

export async function DELETE(req: Request): Promise<Response> {
  return handleWrite(req, env(), "watchlist");
}
