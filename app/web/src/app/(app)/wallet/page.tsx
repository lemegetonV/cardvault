"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { call, errorMessage, newIdempotencyKey } from "@/lib/client/api";
import { formatExpiry, label } from "@/lib/client/format";
import type { Wallet, WalletCard } from "@/lib/client/types";
import { ConsentDialog } from "@/components/consent";
import { Dialog } from "@/components/dialog";
import { useConfig, useToast } from "@/components/providers";
import { Badge, Empty, ErrorState, Loading } from "@/components/states";

// Spec 16.8 rule 54: poll a PENDING card every 2 s, give up after 60 s.
const POLL_MS = 2000;
const POLL_LIMIT_MS = 60_000;
const IN_WALLET = ["PENDING", "ENROLLED", "SUSPENDED"];

type Consent = { kind: "opt-in" } | { kind: "add"; card: WalletCard };

export default function WalletPage() {
  const { operatorUrl } = useConfig();
  const toast = useToast();
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [consent, setConsent] = useState<Consent | null>(null);
  const [confirmOptOut, setConfirmOptOut] = useState(false);
  const [stillPending, setStillPending] = useState<Record<string, boolean>>({});
  const pollStart = useRef<Record<string, number>>({});

  const load = useCallback(async () => {
    setError(null);
    try {
      setWallet(await call<Wallet>("/api/wallet"));
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const replaceCard = useCallback((card: WalletCard) => {
    setWallet((w) =>
      w ? { ...w, cards: w.cards.map((c) => (c.card_id === card.card_id ? card : c)) } : w,
    );
  }, []);

  // The PENDING badge turns into ENROLLED (or FAILED) without a reload.
  const pendingIds = (wallet?.cards ?? [])
    .filter((c) => c.enrollment?.status === "PENDING" && !stillPending[c.card_id])
    .map((c) => c.card_id)
    .join(",");
  useEffect(() => {
    if (!pendingIds) return;
    const ids = pendingIds.split(",");
    const now = Date.now();
    for (const id of ids) pollStart.current[id] ??= now;
    const timer = setInterval(async () => {
      for (const id of ids) {
        if (Date.now() - pollStart.current[id] >= POLL_LIMIT_MS) {
          setStillPending((s) => ({ ...s, [id]: true }));
          continue;
        }
        try {
          replaceCard(await call<WalletCard>(`/api/wallet/cards/${id}`));
        } catch {
          // keep polling; a failed poll is shown by the next successful one
        }
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [pendingIds, replaceCard]);

  const startPolling = (cardId: string) => {
    pollStart.current[cardId] = Date.now();
    setStillPending((s) => ({ ...s, [cardId]: false }));
  };

  async function run(key: string, action: () => Promise<void>, done: string) {
    setBusy(key);
    try {
      await action();
      toast("success", done);
      return true;
    } catch (err) {
      toast("error", errorMessage(err));
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function confirmConsent(version: string) {
    if (!consent) return;
    const ok =
      consent.kind === "add"
        ? await run(
            "consent",
            async () => {
              const card = await call<WalletCard>(`/api/wallet/cards/${consent.card.card_id}`, {
                json: { terms_version: version },
                headers: { "idempotency-key": newIdempotencyKey() },
              });
              startPolling(card.card_id);
              replaceCard(card);
            },
            `Adding the card ending ${consent.card.last4}`,
          )
        : await run(
            "consent",
            async () => {
              const w = await call<Wallet>("/api/wallet/opt-in", {
                json: { terms_version: version },
                headers: { "idempotency-key": newIdempotencyKey() },
              });
              w.cards.forEach((c) => c.enrollment?.status === "PENDING" && startPolling(c.card_id));
              setWallet(w);
            },
            "You are back in the wallet",
          );
    if (ok) setConsent(null);
  }

  async function optOut() {
    const ok = await run(
      "opt-out",
      async () => {
        setWallet(
          await call<Wallet>("/api/wallet/opt-out", {
            method: "POST",
            headers: { "idempotency-key": newIdempotencyKey() },
          }),
        );
      },
      "You opted out of the wallet",
    );
    if (ok) setConfirmOptOut(false);
  }

  async function remove(card: WalletCard) {
    await run(
      `remove:${card.card_id}`,
      async () =>
        replaceCard(
          await call<WalletCard>(`/api/wallet/cards/${card.card_id}`, {
            method: "DELETE",
            headers: { "idempotency-key": newIdempotencyKey() },
          }),
        ),
      `Card ending ${card.last4} removed from the wallet`,
    );
  }

  return (
    <section aria-labelledby="wallet-title">
      <div className="row page-head">
        <h1 id="wallet-title">Checkout wallet</h1>
        <div className="actions">
          <a
            href={`${operatorUrl}/help`}
            target="_blank"
            rel="noopener noreferrer"
            className="link"
            data-testid="wallet-help"
          >
            Wallet help
          </a>
          <Link href="/wallet/transactions" className="link" data-testid="wallet-transactions-link">
            Transactions
          </Link>
        </div>
      </div>
      {error ? (
        <ErrorState message={error} onRetry={load} />
      ) : wallet === null ? (
        <Loading what="Loading your wallet" />
      ) : (
        <>
          <div className="card row" data-testid="wallet-opt-status">
            {wallet.opted_out ? (
              <>
                <span data-testid="opted-out-notice">You have opted out of the wallet.</span>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy !== null}
                  onClick={() => setConsent({ kind: "opt-in" })}
                  data-testid="opt-in"
                >
                  Opt in
                </button>
              </>
            ) : (
              <>
                <span>Cards in the wallet can be used at checkout.</span>
                <button
                  type="button"
                  className="btn btn-danger"
                  disabled={busy !== null}
                  onClick={() => setConfirmOptOut(true)}
                  data-testid="opt-out"
                >
                  Opt out
                </button>
              </>
            )}
          </div>
          {wallet.cards.length === 0 ? (
            <Empty>You have no cards.</Empty>
          ) : (
            <ul className="list" data-testid="wallet-cards">
              {wallet.cards.map((c) => {
                const status = c.enrollment?.status;
                const inWallet = status !== undefined && IN_WALLET.includes(status);
                return (
                  <li key={c.card_id} className="card" data-testid={`wallet-card-${c.card_id}`}>
                    <div className="row">
                      <strong>
                        {c.product} •••• {c.last4}
                      </strong>
                      <span className="muted">Expires {formatExpiry(c.expiry)}</span>
                      {status ? (
                        <Badge value={status} testId={`enrollment-status-${c.card_id}`} />
                      ) : (
                        <Badge value="NOT_ADDED" testId={`enrollment-status-${c.card_id}`} />
                      )}
                    </div>
                    {!c.eligible && (
                      <p className="muted" data-testid={`ineligible-reason-${c.card_id}`}>
                        Not eligible: {label(c.ineligible_reason)}
                      </p>
                    )}
                    {c.enrollment?.status_reason && status !== "ENROLLED" && (
                      <p className="muted" data-testid={`status-reason-${c.card_id}`}>
                        Reason: {label(c.enrollment.status_reason)}
                      </p>
                    )}
                    {status === "PENDING" && stillPending[c.card_id] && (
                      <p className="notice" data-testid={`still-pending-${c.card_id}`}>
                        Still pending. Check again later.
                      </p>
                    )}
                    <div className="actions">
                      {inWallet ? (
                        <button
                          type="button"
                          className="btn"
                          disabled={busy !== null}
                          onClick={() => remove(c)}
                          data-testid={`remove-${c.card_id}`}
                        >
                          {busy === `remove:${c.card_id}` ? "Removing…" : "Remove from wallet"}
                        </button>
                      ) : (
                        c.eligible && (
                          <button
                            type="button"
                            className="btn btn-primary"
                            disabled={busy !== null}
                            onClick={() => setConsent({ kind: "add", card: c })}
                            data-testid={`add-${c.card_id}`}
                          >
                            Add to wallet
                          </button>
                        )
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
      {consent && (
        <ConsentDialog
          title={
            consent.kind === "add"
              ? `Add the card ending ${consent.card.last4}`
              : "Opt in to the wallet"
          }
          confirmLabel={consent.kind === "add" ? "Add card" : "Opt in"}
          busy={busy === "consent"}
          onConfirm={confirmConsent}
          onCancel={() => setConsent(null)}
        />
      )}
      {confirmOptOut && (
        <Dialog
          title="Opt out of the wallet?"
          onClose={busy ? undefined : () => setConfirmOptOut(false)}
          testId="opt-out-dialog"
        >
          <p>Every card is removed from the wallet straight away. You can opt in again later.</p>
          <div className="actions">
            <button
              type="button"
              className="btn"
              disabled={busy !== null}
              onClick={() => setConfirmOptOut(false)}
              data-testid="opt-out-cancel"
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={busy !== null}
              onClick={optOut}
              data-testid="opt-out-confirm"
            >
              {busy === "opt-out" ? "Opting out…" : "Opt out"}
            </button>
          </div>
        </Dialog>
      )}
    </section>
  );
}
