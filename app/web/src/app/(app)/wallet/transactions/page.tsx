"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { call, errorMessage } from "@/lib/client/api";
import { formatCents, formatDateTime, label, localDayBound } from "@/lib/client/format";
import { TXN_STATUSES, type Card, type Transaction } from "@/lib/client/types";
import { Merchant } from "@/components/merchant";
import { Badge, Empty, ErrorState, Loading } from "@/components/states";

interface Filters {
  cards: string[];
  status: string;
  from: string; // local dates, YYYY-MM-DD
  to: string;
}

function readFilters(params: URLSearchParams): Filters {
  return {
    cards: params.getAll("card_id"),
    status: params.get("status") ?? "",
    from: params.get("from") ?? "",
    to: params.get("to") ?? "",
  };
}

/** The query the services see: dates become UTC bounds of the local days (rule 55). */
function apiQuery(f: Filters): URLSearchParams {
  const q = new URLSearchParams();
  f.cards.forEach((c) => q.append("card_id", c));
  if (f.status) q.set("status", f.status);
  if (f.from) q.set("from", localDayBound(f.from, false));
  if (f.to) q.set("to", localDayBound(f.to, true));
  return q;
}

/** The page URL: the filters as the customer chose them (spec 6.6 feature 7). */
function pageQuery(f: Filters): string {
  const q = new URLSearchParams();
  f.cards.forEach((c) => q.append("card_id", c));
  if (f.status) q.set("status", f.status);
  if (f.from) q.set("from", f.from);
  if (f.to) q.set("to", f.to);
  return q.toString();
}

function TransactionsView() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const applied = readFilters(params);
  const appliedKey = pageQuery(applied);

  const [form, setForm] = useState<Filters>(applied);
  const [cards, setCards] = useState<Card[]>([]);
  const [items, setItems] = useState<Transaction[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    call<{ items: Card[] }>("/api/cards?limit=100")
      .then((p) => setCards(p.items))
      .catch(() => setCards([]));
  }, []);

  const load = useCallback(async () => {
    const f = readFilters(new URLSearchParams(appliedKey));
    setForm(f);
    setItems(null);
    setError(null);
    try {
      const q = apiQuery(f);
      q.set("limit", "20");
      const page = await call<{ items: Transaction[]; next_cursor: string | null }>(
        `/api/wallet/transactions?${q}`,
      );
      setItems(page.items);
      setCursor(page.next_cursor);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [appliedKey]);
  useEffect(() => {
    void load();
  }, [load]);

  async function loadMore() {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const q = apiQuery(applied);
      q.set("limit", "20");
      q.set("cursor", cursor);
      const page = await call<{ items: Transaction[]; next_cursor: string | null }>(
        `/api/wallet/transactions?${q}`,
      );
      setItems((xs) => [...(xs ?? []), ...page.items]);
      setCursor(page.next_cursor);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  }

  function apply(e: React.FormEvent) {
    e.preventDefault();
    if (form.from && form.to && form.from > form.to) {
      setFormError("The From date must not be after the To date.");
      return;
    }
    setFormError(null);
    const qs = pageQuery(form);
    router.replace(qs ? `${pathname}?${qs}` : pathname);
  }

  function clear() {
    setFormError(null);
    router.replace(pathname);
  }

  function exportCsv() {
    // Spec 6.6 feature 4: download the export for the filters currently applied.
    const q = apiQuery(applied);
    if (applied.from) q.set("from_date", applied.from);
    if (applied.to) q.set("to_date", applied.to);
    const a = document.createElement("a");
    a.href = `/api/wallet/transactions/export?${q}`;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  const cardName = (id: string) => {
    const c = cards.find((x) => x.card_id === id);
    return c ? `${c.product} •••• ${c.last4}` : "Card";
  };

  return (
    <section aria-labelledby="txn-title">
      <div className="row page-head">
        <h1 id="txn-title">Wallet transactions</h1>
        <div className="actions">
          <button
            type="button"
            className="btn filters-button"
            aria-expanded={filtersOpen}
            aria-controls="txn-filters"
            onClick={() => setFiltersOpen((o) => !o)}
            data-testid="filters-button"
          >
            Filters
          </button>
          <button type="button" className="btn" onClick={exportCsv} data-testid="export-csv">
            Export CSV
          </button>
        </div>
      </div>
      <form
        id="txn-filters"
        className={`card form filters ${filtersOpen ? "filters-open" : ""}`}
        onSubmit={apply}
        aria-label="Filters"
        data-testid="txn-filters"
      >
        <fieldset>
          <legend>Cards</legend>
          {cards.map((c) => (
            <label key={c.card_id} className="check">
              <input
                type="checkbox"
                checked={form.cards.includes(c.card_id)}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    cards: e.target.checked
                      ? [...f.cards, c.card_id]
                      : f.cards.filter((x) => x !== c.card_id),
                  }))
                }
                data-testid={`filter-card-${c.card_id}`}
              />
              {c.product} •••• {c.last4}
            </label>
          ))}
        </fieldset>
        <label htmlFor="filter-status">Status</label>
        <select
          id="filter-status"
          value={form.status}
          onChange={(e) => setForm((f) => ({ ...f, status: e.target.value }))}
          data-testid="filter-status"
        >
          <option value="">All statuses</option>
          {TXN_STATUSES.map((s) => (
            <option key={s} value={s}>
              {label(s)}
            </option>
          ))}
        </select>
        <div className="dates">
          <span>
            <label htmlFor="filter-from">From</label>
            <input
              id="filter-from"
              type="date"
              value={form.from}
              onChange={(e) => setForm((f) => ({ ...f, from: e.target.value }))}
              data-testid="filter-from"
            />
          </span>
          <span>
            <label htmlFor="filter-to">To</label>
            <input
              id="filter-to"
              type="date"
              value={form.to}
              onChange={(e) => setForm((f) => ({ ...f, to: e.target.value }))}
              data-testid="filter-to"
            />
          </span>
        </div>
        {formError && (
          <p className="error" role="alert" data-testid="filter-error">
            {formError}
          </p>
        )}
        <div className="actions">
          <button type="button" className="btn" onClick={clear} data-testid="filter-clear">
            Clear
          </button>
          <button type="submit" className="btn btn-primary" data-testid="filter-apply">
            Apply
          </button>
        </div>
      </form>

      {error ? (
        <ErrorState message={error} onRetry={load} />
      ) : items === null ? (
        <Loading what="Loading transactions" />
      ) : items.length === 0 ? (
        <Empty>No transactions match these filters.</Empty>
      ) : (
        <>
          <ul className="list" data-testid="txn-list">
            {items.map((t) => (
              <li key={t.txn_id} className="card txn" data-testid="txn-row">
                <Link
                  href={`/wallet/transactions/${t.txn_id}`}
                  className="txn-link"
                  data-testid={`txn-${t.txn_id}`}
                >
                  <Merchant name={t.merchant_name} testId="txn-merchant" className="merchant" />
                  <span className="muted" data-testid="txn-time">
                    {formatDateTime(t.occurred_at)} · {cardName(t.card_id)}
                  </span>
                </Link>
                <span className="amount" data-testid="txn-amount">
                  {formatCents(t.amount ?? t.cleared_amount)}
                </span>
                <Badge value={t.status} testId="txn-status" />
                {t.open_dispute_id && <Badge value="DISPUTE_OPEN" testId="txn-dispute-badge" />}
              </li>
            ))}
          </ul>
          {cursor && (
            <button
              type="button"
              className="btn load-more"
              onClick={loadMore}
              disabled={loadingMore}
              data-testid="load-more"
            >
              {loadingMore ? "Loading…" : "Load more"}
            </button>
          )}
        </>
      )}
    </section>
  );
}

export default function TransactionsPage() {
  return (
    <Suspense fallback={<Loading what="Loading transactions" />}>
      <TransactionsView />
    </Suspense>
  );
}
