import * as RDialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { RowsContext } from "../kit/FormRow";
import { IconButton } from "../kit/IconButton";
import { SCRIM_CLASS, ScrimMark } from "./Scrim";

// A dialog for work that is not a yes or no question, and a sheet that slides in from the right edge. Both trap focus, close on Esc and return focus to what opened them (Radix).
// The dialog is a chamfered console window with corner brackets and a header strip led by a diamond; the sheet is a full-height panel with the same header.

type Props = { open: boolean; onOpenChange: (open: boolean) => void; title: string; description?: string; children: ReactNode; className?: string;
  bodyClassName?: string; onCloseAutoFocus?: RDialog.DialogContentProps["onCloseAutoFocus"];
  /** False for work that must run to a conclusion: hides the Close button and ignores Esc and an outside click. Defaults to true. */
  dismissible?: boolean };

function Frame({ open, onOpenChange, title, description, children, className, bodyClassName, onCloseAutoFocus, side, dismissible = true }: Props & { side?: "right" }) {
  // The dialog variant is centred with flexbox, not a translate transform: a clipped (chamfer-r), transformed element renders its text noticeably softer in the desktop shell's
  // webview than in a browser (it falls back from subpixel to grayscale antialiasing), most visible on a block of wrapped text like a textarea. The sheet never used a transform.
  const block = (e: { preventDefault: () => void }) => e.preventDefault();
  const content = (
    <RDialog.Content onEscapeKeyDown={dismissible ? undefined : block} onPointerDownOutside={dismissible ? undefined : block} onInteractOutside={dismissible ? undefined : block} onCloseAutoFocus={onCloseAutoFocus}
      className={cn("border border-line-strong bg-surface shadow-pop outline-none",
        side === "right" ? "fixed inset-y-0 right-0 z-dialog w-[min(28rem,92vw)] border-y-0 border-r-0"
          : "hud-brackets chamfer-r flex max-h-[92vh] w-[min(36rem,94vw)] flex-col [--ch-line:var(--border-strong)]", className)}>
      <div className="flex shrink-0 items-start justify-between gap-4 border-b border-line bg-surface-2 px-5 py-3.5">
        <div className="min-w-0">
          <RDialog.Title className="flex items-center gap-2.5 text-[1rem] font-semibold tracking-[-0.01em]">
            <span aria-hidden className="h-2 w-2 shrink-0 rotate-45 bg-brand-strong" />{title}
          </RDialog.Title>
          {description ? <RDialog.Description className="mt-1 text-[0.8rem] text-muted">{description}</RDialog.Description>
            : <RDialog.Description className="sr-only">{title}</RDialog.Description>}
        </div>
        {dismissible && <RDialog.Close asChild><IconButton label="Close" tip={false} size="sm"><X size={16} aria-hidden /></IconButton></RDialog.Close>}
      </div>
      <RowsContext.Provider value={false}>
        <div className={cn("overflow-y-auto p-5", side === "right" ? "h-[calc(100%-4.5rem)]" : "min-h-0 flex-1", bodyClassName)}>{children}</div>
      </RowsContext.Provider>
    </RDialog.Content>
  );
  return (
    <RDialog.Root open={open} onOpenChange={onOpenChange}>
      {open && <ScrimMark />}
      <RDialog.Portal>
        <RDialog.Overlay className={SCRIM_CLASS} />
        {side === "right" ? content : <div className="fixed inset-0 z-dialog flex items-center justify-center p-4">{content}</div>}
      </RDialog.Portal>
    </RDialog.Root>
  );
}

export const Dialog = (props: Props) => <Frame {...props} />;
export const Sheet = (props: Props) => <Frame {...props} side="right" />;
