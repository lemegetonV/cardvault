"use client";

// Visible loading, empty and error states (spec 6.6), so injected faults show in the UI.

export function Loading({ what = "Loading" }: { what?: string }) {
  return (
    <p className="state" role="status" aria-busy="true" data-testid="loading">
      {what}…
    </p>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="state" data-testid="empty-state">
      {children}
    </p>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state state-error" role="alert" data-testid="error-state">
      <p>{message}</p>
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry} data-testid="retry">
          Try again
        </button>
      )}
    </div>
  );
}

export function Badge({ value, testId }: { value: string; testId?: string }) {
  return (
    <span className={`badge badge-${value.toLowerCase()}`} data-testid={testId}>
      {value.replace(/_/g, " ")}
    </span>
  );
}
