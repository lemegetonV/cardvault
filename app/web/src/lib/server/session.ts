import { NextRequest, NextResponse } from "next/server";
import { cookieSecure, SESSION_SECONDS } from "./config";

export const ACCESS_COOKIE = "cv_access";
export const REFRESH_COOKIE = "cv_refresh";
export const SESSION_COOKIE = "cv_session_exp"; // epoch ms when the session ends
export const CSRF_COOKIE = "cv_csrf";
export const CSRF_HEADER = "x-csrf-token";

const base = { httpOnly: true, sameSite: "lax" as const, path: "/", secure: cookieSecure };

export interface Tokens {
  access_token: string;
  refresh_token: string;
  refresh_expires_in: number;
}

/** Store a fresh token pair; the 15-minute session restarts from now. */
export function setSession(res: NextResponse, tokens: Tokens): void {
  const expiresAt = Date.now() + SESSION_SECONDS * 1000;
  res.cookies.set(ACCESS_COOKIE, tokens.access_token, { ...base, maxAge: SESSION_SECONDS });
  res.cookies.set(REFRESH_COOKIE, tokens.refresh_token, {
    ...base,
    path: "/api/auth",
    maxAge: tokens.refresh_expires_in,
  });
  res.cookies.set(SESSION_COOKIE, String(expiresAt), { ...base, maxAge: SESSION_SECONDS });
}

export function clearSession(res: NextResponse): void {
  for (const name of [ACCESS_COOKIE, SESSION_COOKIE]) {
    res.cookies.set(name, "", { ...base, maxAge: 0 });
  }
  res.cookies.set(REFRESH_COOKIE, "", { ...base, path: "/api/auth", maxAge: 0 });
}

export function secondsLeft(req: NextRequest): number {
  const exp = Number(req.cookies.get(SESSION_COOKIE)?.value ?? 0);
  return Math.max(0, Math.floor((exp - Date.now()) / 1000));
}

/** Double-submit CSRF check for every state-changing BFF call (spec 16.6 rule 41). */
export function csrfOk(req: NextRequest): boolean {
  const cookie = req.cookies.get(CSRF_COOKIE)?.value;
  const header = req.headers.get(CSRF_HEADER);
  return Boolean(cookie) && cookie === header;
}

export function errorJson(status: number, error_code: string, message: string): NextResponse {
  return NextResponse.json({ error_code, message }, { status });
}

export function correlationId(req: NextRequest): string {
  return req.headers.get("x-correlation-id") ?? `web-${crypto.randomUUID()}`;
}
