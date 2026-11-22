import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { Check, Info, TriangleAlert, X } from "lucide-react";
import { useDevMode } from "../lib/devMode";
import { IconButton } from "./kit/IconButton";

type Kind = "success" | "error" | "info";
type Toast = { id: number; kind: Kind; message: string; details?: string };
type ToastApi = {
  success: (m: string) => void;
  /** `details` is the technical text (status, body, stack); it is shown only in Developer mode. */
  error: (m: string, details?: string) => void;
  info: (m: string) => void;
};

const Ctx = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const api = useContext(Ctx);
  if (!api) throw new Error("useToast must be used within <ToastProvider>");
  return api;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seq = useRef(0);

  const remove = useCallback((id: number) => {
    setToasts((t) => t.filter((x) => x.id !== id));
  }, []);

  const push = useCallback((kind: Kind, message: string, details?: string) => {
    const id = ++seq.current;
    setToasts((t) => [...t, { id, kind, message, details }]);
    setTimeout(() => remove(id), kind === "error" ? (details ? 12000 : 6000) : 3500);
  }, [remove]);

  const api = useRef<ToastApi>({
    success: (m) => push("success", m),
    error: (m, d) => push("error", m, d),
    info: (m) => push("info", m),
  }).current;

  return (
    <Ctx.Provider value={api}>
      {children}
      <div className="pointer-events-none fixed bottom-5 right-5 z-toast flex w-[22rem] max-w-[calc(100vw-2.5rem)] flex-col gap-2">
        {toasts.map((t) => <ToastCard key={t.id} toast={t} onClose={() => remove(t.id)} />)}
      </div>
    </Ctx.Provider>
  );
}

// A toast is a chamfered card with a bar down its left edge in the colour of its kind and an icon that says the same in shape (a check, a triangle, an i): colour is never the only signal.
// Success is neutral on purpose (a check mark, not a green light); an error is the critical red as text and bar only.
const STYLES: Record<Kind, { card: string; bar: string; icon: ReactNode; word: string }> = {
  success: { card: "border-ok-line bg-surface-2 [--ch-line:var(--ok-line)]", bar: "bg-ok", icon: <Check size={15} className="text-ok" aria-hidden />, word: "Done" },
  error: { card: "border-crit-line bg-crit-tint [--ch-line:var(--crit-line)]", bar: "bg-crit", icon: <TriangleAlert size={15} className="text-crit-soft" aria-hidden />, word: "Problem" },
  info: { card: "border-line-strong bg-surface-2 [--ch-line:var(--border-strong)]", bar: "bg-info", icon: <Info size={15} className="text-info" aria-hidden />, word: "Note" },
};

function ToastCard({ toast, onClose }: { toast: Toast; onClose: () => void }) {
  const s = STYLES[toast.kind];
  const [dev] = useDevMode();
  return (
    <div role="status" className={`chamfer-r-sm pointer-events-auto relative flex items-start gap-2.5 border py-2.5 pl-4 pr-2 shadow-card toast-in ${s.card}`}>
      <span aria-hidden className={`absolute inset-y-0 left-0 w-1 ${s.bar}`} />
      <span className="mt-0.5">{s.icon}</span>
      <span className="flex-1 break-words text-[0.82rem] leading-snug text-fg">
        <span className="caps mb-0.5 block text-faint">{s.word}</span>
        {toast.message}
        {dev && toast.details && (
          <details className="mt-1.5 text-[0.74rem] text-muted"><summary className="cursor-pointer select-none">Technical details</summary>
            <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words font-mono text-[0.72rem]">{toast.details}</pre></details>)}
      </span>
      <IconButton label="Dismiss" tip={false} size="sm" onClick={onClose}><X size={14} aria-hidden /></IconButton>
    </div>
  );
}
