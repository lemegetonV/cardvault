"use client";

import { useCallback, useEffect, useState } from "react";
import { call, errorMessage, newIdempotencyKey } from "@/lib/client/api";
import { formatCents, formatDateTime, formatExpiry } from "@/lib/client/format";
import { useToast } from "@/components/providers";
import { Badge, Empty, ErrorState, Loading } from "@/components/states";
import type { Card } from "@/lib/client/types";


type Action = "report-lost" | "reissue" | "close";
const DONE: Record<Action, string> = {
  "report-lost": "Card reported lost",
  reissue: "A new card is on its way",
  close: "Card closed",
};

export default function CardsPage() {
  const [cards, setCards] = useState<Card[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const toast = useToast();

  const load = useCallback(async () => {
    setError(null);
    try {
      const page = await call<{ items: Card[] }>("/api/cards?limit=100");
      setCards(page.items);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  async function act(card: Card, action: Action) {
    // Spec 6.6 feature 3: closing uses the browser's native confirm; cancel changes nothing.
    if (
      action === "close" &&
      !window.confirm(`Close the ${card.product.toLowerCase()} card ending ${card.last4}? This cannot be undone.`)
    ) {
      return;
    }
    setBusy(`${card.card_id}:${action}`);
    try {
      const updated = await call<Card>(`/api/cards/${card.card_id}/${action}`, {
        method: "POST",
        headers: { "idempotency-key": newIdempotencyKey() },
      });
      setCards((cs) => cs?.map((c) => (c.card_id === updated.card_id ? updated : c)) ?? null);
      toast("success", DONE[action]);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section aria-labelledby="cards-title">
      <h1 id="cards-title">Cards</h1>
      {error ? (
        <ErrorState message={error} onRetry={load} />
      ) : cards === null ? (
        <Loading what="Loading cards" />
      ) : cards.length === 0 ? (
        <Empty>You have no cards.</Empty>
      ) : (
        <ul className="list" data-testid="card-list">
          {cards.map((c) => (
            <li key={c.card_id} className="card" data-testid={`card-${c.card_id}`}>
              <div className="row">
                <strong data-testid="card-name">
                  {c.product} •••• {c.last4}
                </strong>
                <Badge value={c.status} testId={`card-status-${c.card_id}`} />
              </div>
              <dl className="facts">
                <dt>Expires</dt>
                <dd data-testid="card-expiry">{formatExpiry(c.expiry)}</dd>
                <dt>Available</dt>
                <dd data-testid="card-available">{formatCents(c.available)}</dd>
                <dt>Limit</dt>
                <dd data-testid="card-limit">{formatCents(c.limit)}</dd>
                <dt>Last used</dt>
                <dd data-testid="card-last-used">{formatDateTime(c.last_used_at) || "Never"}</dd>
              </dl>
              {c.status !== "CLOSED" && (
                <div className="actions">
                  {c.status === "ACTIVE" && (
                    <button
                      type="button"
                      className="btn"
                      disabled={busy !== null}
                      onClick={() => act(c, "report-lost")}
                      data-testid={`report-lost-${c.card_id}`}
                    >
                      {busy === `${c.card_id}:report-lost` ? "Reporting…" : "Report lost"}
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn"
                    disabled={busy !== null}
                    onClick={() => act(c, "reissue")}
                    data-testid={`reissue-${c.card_id}`}
                  >
                    {busy === `${c.card_id}:reissue` ? "Reissuing…" : "Reissue"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger"
                    disabled={busy !== null}
                    onClick={() => act(c, "close")}
                    data-testid={`close-${c.card_id}`}
                  >
                    {busy === `${c.card_id}:close` ? "Closing…" : "Close card"}
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
