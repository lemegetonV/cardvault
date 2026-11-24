"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { ApiFailure, call } from "@/lib/client/api";

const REASONS: Record<string, string> = {
  timeout: "You were signed out after 15 minutes.",
  expired: "Your session has ended. Please sign in again.",
};

function safeNext(value: string | null): string {
  // Only same-site paths: never redirect to another origin after login.
  return value && value.startsWith("/") && !value.startsWith("//") ? value : "/wallet";
}

function LoginForm() {
  const params = useSearchParams();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reason = REASONS[params.get("reason") ?? ""];

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await call("/api/auth/login", { json: { username, password } });
      window.location.assign(safeNext(params.get("next")));
    } catch (err) {
      const code = err instanceof ApiFailure ? err.error.error_code : "";
      setError(
        code === "INVALID_CREDENTIALS"
          ? "Wrong username or password."
          : code === "ACCOUNT_LOCKED"
            ? "Too many attempts. The account is locked for a while."
            : err instanceof ApiFailure
              ? `${err.error.message} (${err.error.error_code})`
              : "Something went wrong",
      );
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <h1>CardVault Bank</h1>
      {reason && (
        <p className="notice" role="status" data-testid="login-reason">
          {reason}
        </p>
      )}
      <form onSubmit={submit} className="card form" aria-label="Sign in" data-testid="login-form">
        <label htmlFor="username">Username</label>
        <input
          id="username"
          name="username"
          autoComplete="username"
          required
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          data-testid="login-username"
        />
        <label htmlFor="password">Password</label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          data-testid="login-password"
        />
        {error && (
          <p className="error" role="alert" data-testid="login-error">
            {error}
          </p>
        )}
        <button type="submit" className="btn btn-primary" disabled={busy} data-testid="login-submit">
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
