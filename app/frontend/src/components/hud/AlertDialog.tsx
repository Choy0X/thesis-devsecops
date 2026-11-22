import * as RAlert from "@radix-ui/react-alert-dialog";
import { Info, TriangleAlert } from "lucide-react";
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Button } from "../kit/Button";
import { Checkbox } from "../kit/Toggles";
import { RowsContext } from "../kit/FormRow";
import { Input } from "../kit/TextField";
import { SCRIM_CLASS, ScrimMark } from "./Scrim";

// Every destructive or costly action asks through this dialog: it says in plain words what will happen and names the action on the button (never "OK"). It is drawn as a console
// alert: a chamfered frame with corner brackets, a small caps header that says what kind of question it is (with an icon, so the kind never rests on colour), and for a critical
// request the hazard stripe along the top and a solid critical-red confirm button. Typing a word is reserved for irreversible bulk actions. A question can offer a "do not ask me
// again" box: when the person confirms with it ticked, `onRemember(true)` is called (the caller stores it; nothing is stored here). It replaces the browser's confirm(), which cannot
// be styled, read by a screen reader as part of the page, or tested.

export type ConfirmOptions = {
  title: string;
  /** What will happen, in plain language. */
  description: ReactNode;
  /** The action on the confirm button, for example "Delete 120 records". */
  confirmLabel: string;
  cancelLabel?: string;
  /** "critical" adds the hazard-stripe border and the critical-red confirm button. */
  tone?: "normal" | "critical";
  /** The word to type before the confirm button works. */
  typeToConfirm?: string;
  /** Offers a checkbox with this label ("Do not ask me again"). */
  rememberLabel?: string;
  /** Called with the checkbox state when the question is confirmed (only when rememberLabel is set). */
  onRemember?: (checked: boolean) => void;
};

type Ask = (options: ConfirmOptions) => Promise<boolean>;
const ConfirmContext = createContext<Ask>(() => Promise.reject(new Error("a question was asked without a ConfirmProvider above it")));

/** `const confirm = useConfirm(); if (await confirm({...})) ...` */
export function useConfirm(): Ask {
  return useContext(ConfirmContext);
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<ConfirmOptions | null>(null);
  const [typed, setTyped] = useState("");
  const [remember, setRemember] = useState(false);
  const resolver = useRef<((ok: boolean) => void) | null>(null);
  const returnTo = useRef<HTMLElement | null>(null);   // the control that was focused when the question was asked: Radix returns focus only to a trigger, and a question asked from code has none

  const ask = useCallback<Ask>((options) => new Promise<boolean>((resolve) => {
    if (resolver.current === null) returnTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    resolver.current?.(false);                       // a second question while one is open dismisses the first
    resolver.current = resolve;
    setTyped("");
    setRemember(false);
    setCurrent(options);
  }), []);
  const finish = (ok: boolean) => {
    if (ok && current?.rememberLabel) current.onRemember?.(remember);
    resolver.current?.(ok);
    resolver.current = null;
    setCurrent(null);
  };
  const value = useMemo(() => ask, [ask]);
  const critical = current?.tone === "critical";
  const blocked = !!current?.typeToConfirm && typed.trim() !== current.typeToConfirm;
  const Kind = critical ? TriangleAlert : Info;

  return (
    <ConfirmContext.Provider value={value}>
      {children}
      <RAlert.Root open={current !== null} onOpenChange={(open) => { if (!open) finish(false); }}>
        {current !== null && <ScrimMark />}
        <RAlert.Portal>
          <RAlert.Overlay className={SCRIM_CLASS} />
          {/* Centred with flexbox, not a translate transform: a clipped (chamfer-r), transformed element renders its text noticeably softer in the desktop shell's webview than in a
              browser (it falls back from subpixel to grayscale antialiasing). */}
          <div className="fixed inset-0 z-dialog flex items-center justify-center p-4">
            <RAlert.Content onCloseAutoFocus={(e) => {
              const back = returnTo.current;
              returnTo.current = null;
              if (back?.isConnected) { e.preventDefault(); back.focus({ preventScroll: true }); }       // however the question ended, the person is where they were; a control that is gone leaves Radix's default
            }}
              className={cn("hud-brackets chamfer-r w-[min(32rem,92vw)] border bg-surface shadow-pop",
              critical ? "border-crit-line [--ch-line:var(--crit-line)]" : "border-line-strong [--ch-line:var(--border-strong)]")}>
              {critical && <div aria-hidden className="hazard h-2" />}
              <div className={cn("caps flex items-center gap-2 border-b px-5 py-2", critical ? "border-crit-line bg-crit-tint text-crit-soft" : "border-line bg-surface-2 text-muted")}>
                <Kind size={13} aria-hidden />{critical ? "Warning" : "Confirm"}
              </div>
              <RowsContext.Provider value={false}>
              <div className="space-y-3 p-5">
                <RAlert.Title className="text-[1rem] font-semibold tracking-[-0.01em]">{current?.title}</RAlert.Title>
                <RAlert.Description asChild><div className="text-[0.85rem] leading-relaxed text-muted">{current?.description}</div></RAlert.Description>
                {current?.typeToConfirm && (
                  <label className="block text-[0.78rem] text-muted">
                    Type <b className="font-mono text-fg">{current.typeToConfirm}</b> to continue
                    <Input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} className="mt-1" inputClassName="font-mono" />
                  </label>
                )}
                {current?.rememberLabel && <Checkbox label={current.rememberLabel} checked={remember} onCheckedChange={setRemember} />}
                <div className="flex justify-end gap-2 pt-2">
                  <RAlert.Cancel asChild><Button variant="subtle">{current?.cancelLabel ?? "Cancel"}</Button></RAlert.Cancel>
                  <RAlert.Action asChild>
                    <Button variant={critical ? "critical" : "primary"} disabled={blocked} onClick={() => finish(true)}>{current?.confirmLabel}</Button>
                  </RAlert.Action>
                </div>
              </div>
              </RowsContext.Provider>
            </RAlert.Content>
          </div>
        </RAlert.Portal>
      </RAlert.Root>
    </ConfirmContext.Provider>
  );
}
