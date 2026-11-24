"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { call, errorMessage, newIdempotencyKey } from "@/lib/client/api";
import { formatCents, formatDateTime, label } from "@/lib/client/format";
import { DISPUTABLE, type Dispute, type Transaction } from "@/lib/client/types";
import { Merchant } from "@/components/merchant";
import { useBug, useToast } from "@/components/providers";
import { Badge, ErrorState, Loading } from "@/components/states";

const REASONS = ["UNRECOGNISED", "DUPLICATE", "WRONG_AMOUNT", "NOT_RECEIVED"];
const NOTE_MAX = 500;

/** Spec 6.6 feature 5: reason, note, optional receipt; success toast, then the badge. */
function DisputeForm({ txn, onOpened }: { txn: Transaction; onOpened: () => void }) {
  const toast = useToast();
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const key = useRef(newIdempotencyKey()); // one key per form, so a retry replays
  // Seeded bug: no busy guard and the button stays enabled, so a double click sends twice.
  const doubleSubmit = useBug("BUG_UI_DOUBLE_SUBMIT");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy && !doubleSubmit) return;
    setBusy(true);
    setError(null);
    const form = new FormData();
    form.set("reason", reason);
    if (note) form.set("note", note);
    const file = fileRef.current?.files?.[0];
    if (file) form.set("receipt", file);
    try {
      await call<Dispute>(`/api/wallet/transactions/${txn.txn_id}/disputes`, {
        form,
        // (the seeded bug also sends a new key each time, so the second request is a 409)
        headers: { "idempotency-key": doubleSubmit ? newIdempotencyKey() : key.current },
      });
      toast("success", "Dispute opened");
      onOpened();
    } catch (err) {
      const message = errorMessage(err);
      setError(message);
      toast("error", message);
      key.current = newIdempotencyKey();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card form" onSubmit={submit} aria-label="Dispute" data-testid="dispute-form">
      <h2>Dispute this transaction</h2>
      <label htmlFor="dispute-reason">Reason</label>
      <select
        id="dispute-reason"
        required
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        data-testid="dispute-reason"
      >
        <option value="" disabled>
          Choose a reason
        </option>
        {REASONS.map((r) => (
          <option key={r} value={r}>
            {label(r)}
          </option>
        ))}
      </select>
      <label htmlFor="dispute-note">Note (optional)</label>
      <textarea
        id="dispute-note"
        maxLength={NOTE_MAX}
        rows={3}
        value={note}
        onChange={(e) => setNote(e.target.value)}
        data-testid="dispute-note"
      />
      <span className="muted" data-testid="dispute-note-count">
        {note.length}/{NOTE_MAX}
      </span>
      <label htmlFor="dispute-receipt">Receipt (optional: PDF, PNG or JPEG, up to 5 MB)</label>
      <input
        id="dispute-receipt"
        type="file"
        accept="application/pdf,image/png,image/jpeg"
        ref={fileRef}
        data-testid="dispute-receipt"
      />
      {error && (
        <p className="error" role="alert" data-testid="dispute-error">
          {error}
        </p>
      )}
      <button type="submit" className="btn btn-primary" disabled={busy && !doubleSubmit} data-testid="dispute-submit">
        {busy ? "Sending…" : "Open dispute"}
      </button>
    </form>
  );
}

export default function TransactionDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [txn, setTxn] = useState<Transaction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      setTxn(await call<Transaction>(`/api/wallet/transactions/${id}`));
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [id]);
  useEffect(() => {
    void load();
  }, [load]);

  const canDispute = txn && DISPUTABLE.includes(txn.status) && !txn.open_dispute_id;
  return (
    <section aria-labelledby="txn-detail-title">
      <Link href="/wallet/transactions" className="link" data-testid="back-to-transactions">
        ← All transactions
      </Link>
      {error ? (
        <ErrorState message={error} onRetry={load} />
      ) : txn === null ? (
        <Loading what="Loading the transaction" />
      ) : (
        <>
          <div className="row page-head">
            <Merchant
              as="h1"
              id="txn-detail-title"
              name={txn.merchant_name}
              testId="txn-detail-merchant"
            />
            <Badge value={txn.status} testId="txn-detail-status" />
            {txn.open_dispute_id && <Badge value="DISPUTE_OPEN" testId="dispute-open-badge" />}
          </div>
          <dl className="card facts" data-testid="txn-detail">
            <dt>Amount</dt>
            <dd data-testid="txn-detail-amount">{formatCents(txn.amount)}</dd>
            <dt>Settled</dt>
            <dd data-testid="txn-detail-cleared">{formatCents(txn.cleared_amount)}</dd>
            <dt>Reversed</dt>
            <dd data-testid="txn-detail-reversed">{formatCents(txn.reversed_amount)}</dd>
            <dt>Refunded</dt>
            <dd data-testid="txn-detail-refunded">{formatCents(txn.refunded_amount)}</dd>
            <dt>Date</dt>
            <dd data-testid="txn-detail-time">{formatDateTime(txn.occurred_at)}</dd>
            {txn.settled_at && (
              <>
                <dt>Settled on</dt>
                <dd data-testid="txn-detail-settled-at">{formatDateTime(txn.settled_at)}</dd>
              </>
            )}
            {txn.decline_code && (
              <>
                <dt>Declined</dt>
                <dd data-testid="txn-detail-decline">{label(txn.decline_code)}</dd>
              </>
            )}
            <dt>Category code</dt>
            <dd>{txn.mcc}</dd>
            <dt>Reference</dt>
            <dd className="mono">{txn.trace_id}</dd>
          </dl>
          {canDispute && !showForm && (
            <button
              type="button"
              className="btn"
              onClick={() => setShowForm(true)}
              data-testid="dispute-button"
            >
              Dispute
            </button>
          )}
          {canDispute && showForm && (
            <DisputeForm
              txn={txn}
              onOpened={() => {
                setShowForm(false);
                void load();
              }}
            />
          )}
        </>
      )}
    </section>
  );
}
