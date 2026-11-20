import { handleWatchlist } from "@/lib/watchlistProxy";

// Same-origin proxy to the engine watchlist. The API key is added here, on the server.
export const dynamic = "force-dynamic";

function env() {
  return {
    HADES_API_URL: process.env.HADES_API_URL,
    HADES_API_KEY: process.env.HADES_API_KEY,
    HADES_PUBLIC_ORIGIN: process.env.HADES_PUBLIC_ORIGIN,
  };
}

export async function POST(req: Request): Promise<Response> {
  return handleWatchlist(req, env());
}

export async function DELETE(req: Request): Promise<Response> {
  return handleWatchlist(req, env());
}
