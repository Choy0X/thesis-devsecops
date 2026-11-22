import * as RTooltip from "@radix-ui/react-tooltip";
import type { ReactNode } from "react";

/** A short explanation on hover and keyboard focus. The trigger must be something focusable. Each tooltip carries its own provider so that it works wherever it is rendered. */
export function Tooltip({ label, children, side = "top", delay = 250 }:
  { label: ReactNode; children: ReactNode; side?: "top" | "right" | "bottom" | "left"; delay?: number }) {
  return (
    <RTooltip.Provider delayDuration={delay}>
      <RTooltip.Root>
        <RTooltip.Trigger asChild>{children}</RTooltip.Trigger>
        <RTooltip.Portal>
          <RTooltip.Content side={side} sideOffset={6}
            className="chamfer-sm z-toast max-w-xs border border-line-strong bg-surface-3 px-2.5 py-1.5 text-[0.74rem] leading-snug text-fg shadow-pop">
            {label}
          </RTooltip.Content>
        </RTooltip.Portal>
      </RTooltip.Root>
    </RTooltip.Provider>
  );
}
