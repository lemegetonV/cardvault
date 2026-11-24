/**
 * The BFF proxy: /api/<resource>/... -> the owning service's /v1/<resource>/...
 *
 * The customer's JWT never reaches the browser: it is read from the httpOnly cookie and
 * sent as a Bearer token. State-changing calls need the double-submit CSRF token.
 * Status, body and the headers tests rely on are passed through unchanged, so a service
 * error (or an injected fault) shows up in the UI exactly as the service sent it.
 */
import { NextRequest, NextResponse } from "next/server";
import { services } from "@/lib/server/config";
import { ACCESS_COOKIE, correlationId, csrfOk, errorJson } from "@/lib/server/session";

export const dynamic = "force-dynamic";

const FORWARD_REQUEST = ["content-type", "if-match", "idempotency-key"];
const FORWARD_RESPONSE = [
  "content-type",
  "content-disposition",
  "etag",
  "idempotent-replayed",
  "x-correlation-id",
  "retry-after",
];

function target(path: string[]): string | null {
  const [head, second] = path;
  const rest = path.map(encodeURIComponent).join("/");
  if (head === "cards") return `${services.card}/v1/${rest}`;
  if (head === "profile") return `${services.profile}/v1/${rest}`;
  if (head === "wallet" && (second === "transactions" || second === "disputes")) {
    return `${services.walletTxn}/v1/${rest}`;
  }
  if (head === "wallet") return `${services.enrollment}/v1/${rest}`;
  return null;
}

/**
 * The export filename names the dates the customer picked in their own timezone. The UI
 * sends them as from_date/to_date next to the UTC bounds; the service only sees the bounds.
 */
function exportLabels(path: string[], query: URLSearchParams): [string, string] | null {
  if (path.join("/") !== "wallet/transactions/export") return null;
  const ok = (v: string | null) => (v && /^\d{4}-\d{2}-\d{2}$/.test(v) ? v : null);
  const labels: [string, string] = [
    ok(query.get("from_date")) ?? (query.get("from") ? "" : "all"),
    ok(query.get("to_date")) ?? (query.get("to") ? "" : "all"),
  ];
  query.delete("from_date");
  query.delete("to_date");
  return labels[0] && labels[1] ? labels : null;
}

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const url = target(path);
  if (!url) return errorJson(404, "NOT_FOUND", "No such API route");
  if (req.method !== "GET" && req.method !== "HEAD" && !csrfOk(req)) {
    return errorJson(403, "CSRF_FAILED", "Missing or wrong CSRF token");
  }
  const token = req.cookies.get(ACCESS_COOKIE)?.value;
  if (!token) return errorJson(401, "UNAUTHORIZED", "Not signed in");

  const query = new URLSearchParams(req.nextUrl.searchParams);
  const labels = exportLabels(path, query);
  const headers: Record<string, string> = {
    authorization: `Bearer ${token}`,
    "x-correlation-id": correlationId(req),
  };
  for (const name of FORWARD_REQUEST) {
    const value = req.headers.get(name);
    if (value) headers[name] = value;
  }
  const qs = query.toString();
  let upstream: Response;
  try {
    upstream = await fetch(qs ? `${url}?${qs}` : url, {
      method: req.method,
      headers,
      body: req.method === "GET" || req.method === "HEAD" ? undefined : await req.arrayBuffer(),
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    return errorJson(502, "UPSTREAM_UNAVAILABLE", "The service is not reachable");
  }
  const out = new Headers();
  for (const name of FORWARD_RESPONSE) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  if (labels) {
    out.set(
      "content-disposition",
      `attachment; filename="wallet-transactions-${labels[0]}-${labels[1]}.csv"`,
    );
  }
  out.set("cache-control", "no-store");
  return new NextResponse(upstream.status === 204 ? null : await upstream.arrayBuffer(), {
    status: upstream.status,
    headers: out,
  });
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as PUT, proxy as DELETE };
