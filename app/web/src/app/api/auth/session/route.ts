import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, secondsLeft } from "@/lib/server/session";

export const dynamic = "force-dynamic";

/** How long the session has left, for the client-side timeout warning. */
export function GET(req: NextRequest) {
  const left = req.cookies.get(ACCESS_COOKIE) ? secondsLeft(req) : 0;
  return NextResponse.json({ authenticated: left > 0, session_expires_in: left });
}
