/**
 * Runs for every page and API request:
 * - sets the CSRF cookie (double submit, readable by the page's script) when missing;
 * - sends the CSP (frame-src allows only the operator's embed pages);
 * - sends signed-out visitors of app pages to /login.
 */
import { NextRequest, NextResponse } from "next/server";

const CSRF_COOKIE = "cv_csrf";
const PUBLIC_PAGES = ["/login"];

function csp(): string {
  const operator = (process.env.OPERATOR_PUBLIC_URL ?? "http://localhost:8101").replace(/\/$/, "");
  return [
    "default-src 'self'",
    // Next.js inlines its bootstrap scripts; no third-party script is ever loaded.
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self'",
    `frame-src ${operator}`,
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
  ].join("; ");
}

export function middleware(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  const isApi = pathname.startsWith("/api/");
  const signedIn = Boolean(req.cookies.get("cv_access"));

  let res: NextResponse;
  if (!isApi && !signedIn && !PUBLIC_PAGES.includes(pathname)) {
    const login = new URL("/login", req.url);
    if (pathname !== "/") login.searchParams.set("next", pathname + search);
    res = NextResponse.redirect(login);
  } else if (!isApi && signedIn && pathname === "/login") {
    res = NextResponse.redirect(new URL("/wallet", req.url));
  } else {
    res = NextResponse.next();
  }
  if (!req.cookies.get(CSRF_COOKIE)) {
    res.cookies.set(CSRF_COOKIE, crypto.randomUUID().replace(/-/g, ""), {
      httpOnly: false,
      sameSite: "lax",
      path: "/",
      secure: (process.env.COOKIE_SECURE ?? "false") === "true",
    });
  }
  res.headers.set("Content-Security-Policy", csp());
  return res;
}

export const config = {
  runtime: "nodejs",
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
