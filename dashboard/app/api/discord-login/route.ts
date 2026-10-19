import { NextRequest, NextResponse } from "next/server";
import { getToken } from "next-auth/jwt";

export const dynamic = "force-dynamic";

async function browserProxy(req: NextRequest) {
  const token = await getToken({ req, secret: process.env.NEXTAUTH_SECRET });
  const owners: string[] = (process.env.OWNER_IDS || "").match(/\d{17,20}/g) || [];
  if (!token?.sub || !owners.includes(token.sub)) {
    return NextResponse.json({ detail: "Only the bot owner can manage its Discord login." }, { status: 403 });
  }
  const publicOrigin = new URL(process.env.NEXTAUTH_URL || req.url).origin;
  if (req.method === "POST" && req.headers.get("origin") !== publicOrigin) {
    return NextResponse.json({ detail: "Open this page on your MidVox website." }, { status: 403 });
  }
  try {
    const body = req.method === "POST" ? await req.text() : undefined;
    if (body && body.length > 8192) {
      return NextResponse.json({ detail: "Browser action is too large." }, { status: 400 });
    }
    const response = await fetch("http://127.0.0.1:8082/discord-browser", {
      method: req.method,
      headers: { "Content-Type": "application/json" },
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(45000),
    });
    return new Response(await response.arrayBuffer(), {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("content-type") || "application/json",
        "Cache-Control": "no-store, private",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch {
    return NextResponse.json({ detail: "The Railway login service is unavailable. Check the deployment logs." }, { status: 502 });
  }
}

export const GET = browserProxy;
export const POST = browserProxy;
