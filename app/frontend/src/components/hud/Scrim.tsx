import { useEffect } from "react";

// The layer behind a dialog. A translucent layer over the whole window made the real desktop window flicker on the graphics card while a dialog was scrolled or typed in: parts of the page
// vanished and came back. It was measured in the real window: an overlay at 80 or 97 percent flickered, one at 0 or 100 percent did not, and neither the
// cut corners, the centring transform, the shadow nor the animations of the dialog had any part in it. So the layer itself is clear (it still catches the pointer and locks the scroll, which
// is Radix's job) and, with the graphics card, the page behind is dimmed by a filter on its root while a dialog is open (index.css: `html[data-scrim] #root`). Without the card
// (data-motion="reduced") a filter is costly to paint and the translucent layer never flickered, so the stylesheet keeps the old look there.

export const SCRIM_CLASS = "scrim";

let open = 0;

/** Marks the document while a dialog is open. Dialogs can stack (a question over a dialog), so the mark stays until the last one closes. Render it inside the dialog's portal. */
export function ScrimMark() {
  useEffect(() => {
    open += 1;
    document.documentElement.dataset.scrim = "on";
    return () => {
      open = Math.max(0, open - 1);
      if (open === 0) delete document.documentElement.dataset.scrim;
    };
  }, []);
  return null;
}
