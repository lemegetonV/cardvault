"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiFailure, call, errorMessage } from "@/lib/client/api";
import type { Profile } from "@/lib/client/types";
import { useToast } from "@/components/providers";
import { Badge, ErrorState, Loading } from "@/components/states";

const COOLDOWN_S = 30; // mirrors the server's cooldown (spec 16.8 rule 52)
type Channel = "EMAIL" | "PHONE";

/** Spec 6.6 feature 6: "Resend" stays disabled for 30 s, with a countdown. */
function Verify({ channel, onVerified }: { channel: Channel; onVerified: () => void }) {
  const toast = useToast();
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [until, setUntil] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState<"send" | "verify" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const left = Math.max(0, Math.ceil((until - now) / 1000));
  const id = channel.toLowerCase();

  useEffect(() => {
    if (until <= Date.now()) return;
    const t = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(t);
  }, [until]);

  async function send() {
    setBusy("send");
    setError(null);
    try {
      const r = await call<{ sent_to: string; cooldown_seconds: number }>(
        "/api/profile/verify/send",
        { json: { channel } },
      );
      setSentTo(r.sent_to);
      setUntil(Date.now() + (r.cooldown_seconds || COOLDOWN_S) * 1000);
      setNow(Date.now());
      toast("success", `Code sent to ${r.sent_to}`);
    } catch (err) {
      if (err instanceof ApiFailure && err.error.error_code === "OTP_COOLDOWN") {
        const d = err.error.details as { retry_after_seconds?: number } | undefined;
        setUntil(Date.now() + (d?.retry_after_seconds ?? COOLDOWN_S) * 1000);
        setNow(Date.now());
      }
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function verify(e: React.FormEvent) {
    e.preventDefault();
    setBusy("verify");
    setError(null);
    try {
      await call("/api/profile/verify", { json: { channel, code } });
      toast("success", `${channel === "EMAIL" ? "Email" : "Phone"} verified`);
      onVerified();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="verify" data-testid={`verify-${id}`}>
      <button
        type="button"
        className="btn"
        disabled={busy !== null || left > 0}
        onClick={send}
        data-testid={`send-code-${id}`}
      >
        {left > 0 ? `Resend in ${left} s` : sentTo ? "Resend code" : "Send code"}
      </button>
      {sentTo && (
        <form onSubmit={verify} className="inline-form" aria-label={`Verify ${id}`}>
          <label htmlFor={`code-${id}`}>Code sent to {sentTo}</label>
          <input
            id={`code-${id}`}
            inputMode="numeric"
            pattern="\d{6}"
            maxLength={6}
            required
            value={code}
            onChange={(e) => setCode(e.target.value)}
            data-testid={`code-${id}`}
          />
          <button
            type="submit"
            className="btn btn-primary"
            disabled={busy !== null}
            data-testid={`verify-submit-${id}`}
          >
            {busy === "verify" ? "Checking…" : "Verify"}
          </button>
        </form>
      )}
      {error && (
        <p className="error" role="alert" data-testid={`verify-error-${id}`}>
          {error}
        </p>
      )}
    </div>
  );
}

export default function ProfilePage() {
  const toast = useToast();
  const [profile, setProfile] = useState<Profile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const p = await call<Profile>("/api/profile");
      setProfile(p);
      setEmail(p.email ?? "");
      setPhone(p.phone ?? "");
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!profile) return;
    const patch: Record<string, string> = {};
    if (email !== (profile.email ?? "")) patch.email = email;
    if (phone !== (profile.phone ?? "")) patch.phone = phone;
    if (Object.keys(patch).length === 0) return;
    setSaving(true);
    setSaveError(null);
    try {
      const p = await call<Profile>("/api/profile", {
        method: "PATCH",
        json: patch,
        headers: { "if-match": String(profile.version) },
      });
      setProfile(p);
      toast("success", "Profile saved. Verify the new details to keep your wallet cards.");
    } catch (err) {
      const message =
        err instanceof ApiFailure && err.error.error_code === "VERSION_MISMATCH"
          ? "Your profile changed somewhere else. Reload and try again."
          : errorMessage(err);
      setSaveError(message);
      toast("error", message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-labelledby="profile-title">
      <h1 id="profile-title">Profile</h1>
      {error ? (
        <ErrorState message={error} onRetry={load} />
      ) : profile === null ? (
        <Loading what="Loading your profile" />
      ) : (
        <>
          <form className="card form" onSubmit={save} aria-label="Contact details" data-testid="profile-form">
            <label htmlFor="profile-email">
              Email <Badge value={profile.email_verified ? "VERIFIED" : "UNVERIFIED"} testId="email-verified" />
            </label>
            <input
              id="profile-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              data-testid="profile-email"
            />
            <label htmlFor="profile-phone">
              Phone <Badge value={profile.phone_verified ? "VERIFIED" : "UNVERIFIED"} testId="phone-verified" />
            </label>
            <input
              id="profile-phone"
              type="tel"
              placeholder="+15550100101"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              data-testid="profile-phone"
            />
            {saveError && (
              <p className="error" role="alert" data-testid="profile-error">
                {saveError}
              </p>
            )}
            <button type="submit" className="btn btn-primary" disabled={saving} data-testid="profile-save">
              {saving ? "Saving…" : "Save"}
            </button>
          </form>
          {!profile.email_verified && profile.email && (
            <div className="card">
              <h2>Verify your email</h2>
              <Verify channel="EMAIL" onVerified={load} />
            </div>
          )}
          {!profile.phone_verified && profile.phone && (
            <div className="card">
              <h2>Verify your phone</h2>
              <Verify channel="PHONE" onVerified={load} />
            </div>
          )}
        </>
      )}
    </section>
  );
}
