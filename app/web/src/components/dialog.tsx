"use client";

import { useEffect, useRef } from "react";

/** A small accessible modal: role="dialog" (or "alertdialog"), labelled, Escape closes. */
export function Dialog({
  title,
  role = "dialog",
  onClose,
  testId,
  children,
}: {
  title: string;
  role?: "dialog" | "alertdialog";
  onClose?: () => void;
  testId: string;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && onClose) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const titleId = `${testId}-title`;
  return (
    <div className="overlay">
      <div
        className="dialog"
        role={role}
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={ref}
        data-testid={testId}
      >
        <h2 id={titleId}>{title}</h2>
        {children}
      </div>
    </div>
  );
}
