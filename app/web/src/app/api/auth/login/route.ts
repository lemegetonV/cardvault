import { NextRequest, NextResponse } from "next/server";
import { services, SESSION_SECONDS } from "@/lib/server/config";
import { correlationId, csrfOk, errorJson, setSession } from "@/lib/server/session";

export async function POST(req: NextRequest) {
  if (!csrfOk(req)) return errorJson(403, "CSRF_FAILED", "Missing or wrong CSRF token");
  let upstream: Response;
  try {
    upstream = await fetch(`${services.auth}/v1/auth/login`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-correlation-id": correlationId(req) },
      body: await req.text(),
      cache: "no-store",
    });
  } catch {
    return errorJson(502, "UPSTREAM_UNAVAILABLE", "The sign-in service is not reachable");
  }
  const data = await upstream.json().catch(() => ({}));
  if (!upstream.ok) return NextResponse.json(data, { status: upstream.status });
  // The tokens stay in httpOnly cookies; the browser only learns how long the session lasts.
  const res = NextResponse.json({
    customer_id: data.customer_id,
    session_expires_in: SESSION_SECONDS,
  });
  setSession(res, data);
  return res;
}
