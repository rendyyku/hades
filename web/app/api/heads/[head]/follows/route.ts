import { handleHeadFollow } from "@/lib/headProxy";

// Same-origin follow/unfollow proxy to a head. The head key is added on the server.
export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ head: string }> };

export async function POST(req: Request, { params }: Ctx): Promise<Response> {
  return handleHeadFollow(req, (await params).head, { env: process.env });
}

export async function DELETE(req: Request, { params }: Ctx): Promise<Response> {
  return handleHeadFollow(req, (await params).head, { env: process.env });
}
