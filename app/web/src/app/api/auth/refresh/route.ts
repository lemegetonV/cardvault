import { NextRequest, NextResponse } from "next/server";
import { services, SESSION_SECONDS } from "@/lib/server/config";
import {
  clearSession,
  correlationId,
  csrfOk,
  errorJson,
  REFRESH_COOKIE,
  setSession,
} from "@/lib/server/session";

/** "Stay signed in": rotate the refresh token and restart the 15-minute session. */
export async function POST(req: NextRequest) {
  if (!csrfOk(req)) return errorJson(403, "CSRF_FAILED", "Missing or wrong CSRF token");
  const refresh = req.cookies.get(REFRESH_COOKIE)?.value;
  if (!refresh) return errorJson(401, "UNAUTHORIZED", "Not signed in");
  let upstream: Response;
  try {
    upstream = await fetch(`${services.auth}/v1/auth/refresh`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-correlation-id": correlationId(req) },
      body: JSON.stringify({ refresh_token: refresh }),
      cache: "no-store",
    });
  } catch {
    return errorJson(502, "UPSTREAM_UNAVAILABLE", "The sign-in service is not reachable");
  }
  const data = await upstream.json().catch(() => ({}));
  if (!upstream.ok) {
    const res = NextResponse.json(data, { status: upstream.status });
    clearSession(res);
    return res;
  }
  const res = NextResponse.json({ session_expires_in: SESSION_SECONDS });
  setSession(res, data);
  return res;
}
