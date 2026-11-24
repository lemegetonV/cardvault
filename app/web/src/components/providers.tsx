"use client";

import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";

interface Config {
  operatorUrl: string;
}

const ConfigContext = createContext<Config>({ operatorUrl: "http://localhost:8101" });
export const useConfig = () => useContext(ConfigContext);

type ToastKind = "success" | "error";
interface Toast {
  id: number;
  kind: ToastKind;
  text: string;
}
const ToastContext = createContext<(kind: ToastKind, text: string) => void>(() => undefined);
export const useToast = () => useContext(ToastContext);

export const TOAST_MS = 4000; // spec 6.6 feature 10

export function Providers({ config, children }: { config: Config; children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);
  const push = useCallback((kind: ToastKind, text: string) => {
    const id = next.current++;
    setToasts((t) => [...t, { id, kind, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), TOAST_MS);
  }, []);
  const value = useMemo(() => config, [config]);
  return (
    <ConfigContext.Provider value={value}>
      <ToastContext.Provider value={push}>
        {children}
        <div className="toasts" data-testid="toasts">
          {toasts.map((t) => (
            <div
              key={t.id}
              role="status"
              aria-live="polite"
              className={`toast toast-${t.kind}`}
              data-testid={`toast-${t.kind}`}
            >
              {t.text}
            </div>
          ))}
        </div>
      </ToastContext.Provider>
    </ConfigContext.Provider>
  );
}
