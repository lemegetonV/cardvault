"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { call } from "@/lib/client/api";
import { Dialog } from "./dialog";
import { useToast } from "./providers";

const LINKS = [
  { href: "/wallet", label: "Wallet" },
  { href: "/wallet/transactions", label: "Transactions" },
  { href: "/cards", label: "Cards" },
  { href: "/profile", label: "Profile" },
];
const WARN_SECONDS = 60; // the warning shows at 14:00 of the 15-minute session

async function signOut(reason?: string) {
  await call("/api/auth/logout", { method: "POST" }).catch(() => undefined);
  window.location.assign(reason ? `/login?reason=${reason}` : "/login");
}

/** Spec 6.6 feature 6: warn at 14:00, sign out at 15:00; "Stay signed in" refreshes. */
function SessionTimer() {
  const deadline = useRef<number | null>(null);
  const [left, setLeft] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    let cancelled = false;
    call<{ session_expires_in: number }>("/api/auth/session").then((s) => {
      if (!cancelled) deadline.current = Date.now() + s.session_expires_in * 1000;
    });
    const timer = setInterval(() => {
      if (deadline.current === null) return;
      const seconds = Math.ceil((deadline.current - Date.now()) / 1000);
      if (seconds <= 0) {
        deadline.current = null;
        clearInterval(timer);
        void signOut("timeout");
        return;
      }
      setLeft(seconds);
    }, 250);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const stay = useCallback(async () => {
    setBusy(true);
    try {
      const s = await call<{ session_expires_in: number }>("/api/auth/refresh", {
        method: "POST",
      });
      deadline.current = Date.now() + s.session_expires_in * 1000;
      setLeft(s.session_expires_in);
      toast("success", "You are still signed in");
    } catch {
      void signOut("expired");
    } finally {
      setBusy(false);
    }
  }, [toast]);

  if (left === null || left > WARN_SECONDS) return null;
  return (
    <Dialog title="Your session is about to end" role="alertdialog" testId="session-warning">
      <p data-testid="session-countdown">Signing out in {left} s</p>
      <div className="actions">
        <button
          type="button"
          className="btn"
          onClick={() => void signOut()}
          data-testid="session-sign-out"
        >
          Sign out
        </button>
        <button
          type="button"
          className="btn btn-primary"
          onClick={stay}
          disabled={busy}
          data-testid="session-stay"
        >
          Stay signed in
        </button>
      </div>
    </Dialog>
  );
}

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  useEffect(() => setMenuOpen(false), [pathname]);
  const active = (href: string) =>
    href === "/wallet" ? pathname === "/wallet" : pathname.startsWith(href);
  return (
    <>
      <header className="header">
        <Link href="/wallet" className="brand" data-testid="brand">
          CardVault Bank
        </Link>
        <button
          type="button"
          className="btn menu-button"
          aria-expanded={menuOpen}
          aria-controls="main-nav"
          onClick={() => setMenuOpen((o) => !o)}
          data-testid="menu-button"
        >
          Menu
        </button>
        <nav
          id="main-nav"
          aria-label="Main"
          className={`nav ${menuOpen ? "nav-open" : ""}`}
          data-testid="main-nav"
        >
          {LINKS.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              aria-current={active(l.href) ? "page" : undefined}
              data-testid={`nav-${l.label.toLowerCase()}`}
            >
              {l.label}
            </Link>
          ))}
          <button
            type="button"
            className="btn"
            disabled={signingOut}
            onClick={() => {
              setSigningOut(true);
              void signOut();
            }}
            data-testid="sign-out"
          >
            Sign out
          </button>
        </nav>
      </header>
      <main className="main">{children}</main>
      <SessionTimer />
    </>
  );
}
