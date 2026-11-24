import { NextRequest, NextResponse } from "next/server";
import { services } from "@/lib/server/config";
import {
  clearSession,
  correlationId,
  csrfOk,
  errorJson,
  REFRESH_COOKIE,
} from "@/lib/server/session";

export async function POST(req: NextRequest) {
  if (!csrfOk(req)) return errorJson(403, "CSRF_FAILED", "Missing or wrong CSRF token");
  const refresh = req.cookies.get(REFRESH_COOKIE)?.value;
  if (refresh) {
    // Best effort: the cookies are cleared even if auth-service is down.
    await fetch(`${services.auth}/v1/auth/logout`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-correlation-id": correlationId(req) },
      body: JSON.stringify({ refresh_token: refresh }),
      cache: "no-store",
    }).catch(() => undefined);
  }
  const res = new NextResponse(null, { status: 204 });
  clearSession(res);
  return res;
}
