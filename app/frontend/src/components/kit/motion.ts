import { useEffect, useState } from "react";

// One question for every effect of the kit (the glitch, the rain, the scramble): may it move? It may not when the person asked their system for less motion
// (prefers-reduced-motion) or when the window is drawn without the graphics card (App.tsx sets data-motion="reduced" from the saved setting). A component that
// moves reads this and shows its still state instead; the stylesheet cuts animations for the same two reasons as a second line of defence.

const QUERY = "(prefers-reduced-motion: reduce)";

export function motionAllowed(): boolean {
  if (typeof document === "undefined") return false;
  if (document.documentElement.dataset.motion === "reduced") return false;
  return !(window.matchMedia?.(QUERY).matches ?? false);
}

/** The answer of `motionAllowed`, kept current when the system setting or the data-motion attribute changes. */
export function useMotionAllowed(): boolean {
  const [allowed, setAllowed] = useState(motionAllowed);
  useEffect(() => {
    const media = window.matchMedia?.(QUERY);
    const update = () => setAllowed(motionAllowed());
    media?.addEventListener?.("change", update);
    const watch = new MutationObserver(update);
    watch.observe(document.documentElement, { attributes: true, attributeFilter: ["data-motion"] });
    update();
    return () => { media?.removeEventListener?.("change", update); watch.disconnect(); };
  }, []);
  return allowed;
}
