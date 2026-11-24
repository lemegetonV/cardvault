"use client";

export interface ApiError {
  status: number;
  error_code: string;
  message: string;
  details?: unknown;
}

export class ApiFailure extends Error {
  constructor(public readonly error: ApiError) {
    super(error.message);
  }
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)cv_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export interface CallOptions {
  method?: string;
  json?: unknown;
  form?: FormData;
  headers?: Record<string, string>;
  /** Return the raw Response instead of parsing JSON (for headers such as ETag). */
  raw?: boolean;
}

/** Call the BFF. Throws ApiFailure with the service's error shape on any non-2xx. */
export async function call<T = unknown>(path: string, opts: CallOptions = {}): Promise<T> {
  const method = opts.method ?? (opts.json !== undefined || opts.form ? "POST" : "GET");
  const headers: Record<string, string> = { ...(opts.headers ?? {}) };
  let body: BodyInit | undefined;
  if (opts.json !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(opts.json);
  } else if (opts.form) {
    body = opts.form; // the browser sets the multipart boundary
  }
  if (method !== "GET") headers["x-csrf-token"] = csrfToken();
  let res: Response;
  try {
    res = await fetch(path, { method, headers, body, cache: "no-store" });
  } catch {
    throw new ApiFailure({ status: 0, error_code: "NETWORK_ERROR", message: "Network error" });
  }
  if (res.status === 401 && !path.startsWith("/api/auth/login")) {
    window.location.assign("/login?reason=expired");
    throw new ApiFailure({ status: 401, error_code: "UNAUTHORIZED", message: "Signed out" });
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new ApiFailure({
      status: res.status,
      error_code: data.error_code ?? "ERROR",
      message: data.message ?? `Request failed (${res.status})`,
      details: data.details,
    });
  }
  if (opts.raw) return res as unknown as T;
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiFailure) return `${err.error.message} (${err.error.error_code})`;
  return "Something went wrong";
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
