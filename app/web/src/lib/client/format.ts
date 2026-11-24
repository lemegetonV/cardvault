"use client";

// Spec 6.6 feature 9: every date and time in the browser's timezone, amounts as USD.
let utcTimes = false; // seeded bug BUG_UI_TIMEZONE_UTC
export function setUtcTimes(on: boolean): void {
  utcTimes = on;
}
const dateTime = () =>
  new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    ...(utcTimes ? { timeZone: "UTC" } : {}),
  });
const usd = () => new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" });

export function formatDateTime(iso: string | null | undefined): string {
  return iso ? dateTime().format(new Date(iso)) : "";
}

/** Integer cents to USD text. Division happens only for display, never for maths. */
export function formatCents(cents: number | null | undefined): string {
  return cents === null || cents === undefined ? "" : usd().format(cents / 100);
}

export function formatExpiry(expiry: string): string {
  const [y, m] = expiry.split("-");
  return `${m}/${y.slice(2)}`;
}

/** A local calendar date (YYYY-MM-DD) to the UTC instant where that day starts or ends. */
export function localDayBound(date: string, end: boolean): string {
  const [y, m, d] = date.split("-").map(Number);
  const local = end ? new Date(y, m - 1, d, 23, 59, 59, 999) : new Date(y, m - 1, d, 0, 0, 0, 0);
  return local.toISOString();
}

export function label(code: string | null | undefined): string {
  return (code ?? "").replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
}
