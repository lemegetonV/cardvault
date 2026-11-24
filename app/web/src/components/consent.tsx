"use client";

import { useEffect, useState } from "react";
import { useConfig } from "./providers";
import { Dialog } from "./dialog";

/**
 * Spec 6.6 feature 1: the wallet terms come from the operator in an iframe. "I agree" and
 * "Continue" are inside the frame; the frame tells us with postMessage, and only then is
 * the confirm button (Opt in / Add card) enabled. Messages from any other origin are ignored.
 */
export function ConsentDialog({
  title,
  confirmLabel,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string;
  confirmLabel: string;
  busy: boolean;
  onConfirm: (termsVersion: string) => void;
  onCancel: () => void;
}) {
  const { operatorUrl } = useConfig();
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    const operatorOrigin = new URL(operatorUrl).origin;
    const onMessage = (e: MessageEvent) => {
      if (e.origin !== operatorOrigin) return;
      const data = e.data as { type?: string; version?: string } | null;
      if (data?.type === "terms-accepted" && typeof data.version === "string") {
        setVersion(data.version);
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [operatorUrl]);
  return (
    <Dialog title={title} onClose={busy ? undefined : onCancel} testId="consent-dialog">
      <iframe
        className="terms-frame"
        title="Wallet terms"
        src={`${operatorUrl}/embed/terms`}
        data-testid="terms-iframe"
      />
      <p className="muted" data-testid="consent-status">
        {version ? `Terms ${version} accepted.` : "Read and accept the terms above to continue."}
      </p>
      <div className="actions">
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!version || busy}
          onClick={() => version && onConfirm(version)}
          data-testid="consent-confirm"
        >
          {busy ? "Working…" : confirmLabel}
        </button>
      </div>
    </Dialog>
  );
}
